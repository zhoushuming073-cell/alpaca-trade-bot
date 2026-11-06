"""Build an isolated historical market exam. GET-only; no bot or AI calls."""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml
from dateutil.relativedelta import relativedelta

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from exam_data.store import ExamStore, sessions, get_exam_snapshot, ET, digest  # noqa:E402

DATA_URL = "https://data.alpaca.markets"


class MarketData:
    def __init__(self, keys, limit=1000):
        self.client = requests.Session()
        self.client.headers.update({"APCA-API-KEY-ID": keys["ALPACA_API_KEY"],
                                    "APCA-API-SECRET-KEY": keys["ALPACA_API_SECRET"]})
        self.calls = 0
        self.limit = limit

    def get(self, path, params):
        if self.calls >= self.limit:
            raise RuntimeError("Explicit API request budget exhausted")
        for attempt in range(3):
            self.calls += 1
            try:
                r = self.client.get(DATA_URL + path, params=params, timeout=(5,25))
                if r.status_code in (429,500,502,503,504) and attempt<2:
                    time.sleep(min(20,2**attempt*3));continue
                if r.status_code in (401,403):
                    raise PermissionError(f"Alpaca historical endpoint unavailable: HTTP {r.status_code}")
                r.raise_for_status()
                return r.json()
            except (requests.Timeout,requests.ConnectionError):
                if attempt==2:raise
                time.sleep(2**attempt)
        raise RuntimeError("API retries exhausted")

    def pages(self, path, params, key):
        params = dict(params)
        seen = set()
        for _ in range(100):
            data=self.get(path,params)
            yield data
            token=data.get("next_page_token")
            if not token:return
            if token in seen:raise ValueError("Repeated page token")
            seen.add(token);params["page_token"]=token
        raise ValueError("More than 100 history pages; job not complete")


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--phase",choices=["inspect","pilot","all","market-news","audit"],required=True)
    parser.add_argument("--db",default=str(ROOT/"data/exam_data.duckdb"))
    parser.add_argument("--max-requests",type=int,default=1000)
    parser.add_argument("--feed",choices=["iex","sip"],default="sip")
    parser.add_argument("--include-news",action="store_true",help="Historical news bulk access must be authorized separately")
    args=parser.parse_args()
    symbols=yaml.safe_load((ROOT/"config/watchlist.yaml").read_text(encoding="utf-8"))["symbols"]
    if len(symbols)!=48 or len(set(symbols))!=48:raise ValueError("Expected 48 unique fixed symbols")
    days=sessions()
    if args.phase=="inspect":
        prior_jobs=0
        if Path(args.db).exists():
            with ExamStore(args.db,read_only=True) as store:
                prior_jobs=store.scalar("SELECT COUNT(*) FROM jobs")
        print(json.dumps({"symbols":len(symbols),"market_reference":"QQQ proxy",
                          "first":str(days[0][0]),"last":str(days[-1][0]),
                          "prior_jobs":prior_jobs,
                          "estimated_api_calls":"daily pages ~2-5; minute pages ~120-300; news separate; Basic 200/min"},ensure_ascii=False))
        return
    if args.phase=="audit":
        with ExamStore(args.db,read_only=True) as store:audit(store,days,symbols,args.feed)
        return
    with ExamStore(args.db) as store:
        if store.db.execute("SELECT value FROM meta WHERE key='freeze_version'").fetchone():
            raise RuntimeError("Exam database is frozen; create a new version before collecting more data")
        old=store.db.execute("SELECT value FROM meta WHERE key='watchlist_hash'").fetchone()
        if old and old[0]!=digest(symbols):raise ValueError("Watchlist changed; create a new exam version")
        store.meta("watchlist_hash",digest(symbols))
        store.meta("feed",args.feed)
        store.meta("adjustment","raw")
        store.meta("index_reference","QQQ Nasdaq-100 ETF proxy; not Nasdaq Composite ^IXIC")
        store.meta("exam_start",days[0][0].isoformat())
        store.meta("exam_end",days[-1][0].isoformat())
        for sym in symbols:store.db.execute("INSERT OR REPLACE INTO symbols VALUES (?,?,?)",[sym,"trade",sym])
        store.db.execute("INSERT OR REPLACE INTO symbols VALUES ('QQQ','market_reference','NASDAQ-100 ETF proxy; not ^IXIC')")
        for day,opening,closing in days:
            store.db.execute("INSERT OR REPLACE INTO days VALUES (?,?,?,?)",[day,opening,closing,opening-timedelta(minutes=30)])
        # Existing bot's credential loader reads the local Paper key without exposing it.
        from trading_bot.dashboard import alpaca_environment
        client=MarketData(alpaca_environment(),args.max_requests)
        chosen=[days[0],days[len(days)//2],days[-1]] if args.phase=="pilot" else days
        universe=symbols+["QQQ"]
        if args.phase=="market-news":
            for day,opening,_ in days:
                key="market-news:"+str(day)
                if store.job_done(key):continue
                start=opening.astimezone(ET).replace(hour=0,minute=0,second=0,microsecond=0)
                end=start+timedelta(days=1)
                count=0
                try:
                    for page in client.pages("/v1beta1/news",{"symbols":"QQQ","start":start.isoformat(),
                                                        "end":end.isoformat(),"limit":50,"sort":"asc",
                                                        "include_content":"true"},"news"):
                        count+=store.put_news_many(page.get("news") or [])
                    store.job(key,"market-news",day,"done",count)
                except Exception as exc:
                    store.job(key,"market-news",day,"failed",count,type(exc).__name__+": "+str(exc))
                    if isinstance(exc,PermissionError):raise
            print(json.dumps({"phase":args.phase,"requests":client.calls,
                              "completed_days":store.scalar("SELECT COUNT(*) FROM jobs WHERE kind='market-news' AND status='done'")}),flush=True)
            return
        # Daily lookback starts six months before the earliest exam day.
        daily_key="daily-all:"+args.feed
        if not store.job_done(daily_key):
            start=days[0][0]-relativedelta(months=6)
            end=days[-1][2]+timedelta(minutes=1)
            params={"symbols":",".join(universe),"timeframe":"1Day","start":str(start),"end":end.isoformat(),
                    "limit":10000,"feed":args.feed,"adjustment":"raw","asof":"-","sort":"asc"}
            count=0
            try:
                for page in client.pages("/v2/stocks/bars",params,"bars"):
                    batch=[(symbol,bar) for symbol,bars in (page.get("bars") or {}).items() if symbol in universe for bar in bars]
                    count+=store.put_bars(batch,"1Day",feed=args.feed)
                store.job(daily_key,"daily",None,"done",count)
            except Exception as exc:
                store.job(daily_key,"daily",None,"failed",count,type(exc).__name__+": "+str(exc));raise
        for day,opening,closing in chosen:
            key="minute:"+args.feed+":"+str(day)
            if not store.job_done(key):
                local_4am=opening.astimezone(ET).replace(hour=4,minute=0,second=0,microsecond=0)
                params={"symbols":",".join(universe),"timeframe":"1Min","start":local_4am.isoformat(),
                        "end":closing.isoformat(),"limit":10000,"feed":args.feed,"adjustment":"raw","asof":"-","sort":"asc"}
                count=0
                try:
                    for page in client.pages("/v2/stocks/bars",params,"bars"):
                        batch=[(symbol,bar) for symbol,bars in (page.get("bars") or {}).items() if symbol in universe for bar in bars]
                        count+=store.put_bars(batch,"1Min",feed=args.feed)
                    store.coverage(day,universe,feed=args.feed)
                    store.job(key,"minute",day,"done",count)
                    print(json.dumps({"day":str(day),"minute_rows":count,"requests":client.calls}),flush=True)
                except Exception as exc:
                    store.job(key,"minute",day,"failed",count,type(exc).__name__+": "+str(exc))
                    print(json.dumps({"day":str(day),"error":type(exc).__name__}),flush=True)
                    if isinstance(exc,PermissionError):raise
            if args.include_news and not store.job_done("news:"+str(day)):
                start=opening.astimezone(ET).replace(hour=0,minute=0,second=0,microsecond=0)
                end=start+timedelta(days=1)
                count=0
                try:
                    for page in client.pages("/v1beta1/news",{"symbols":",".join(symbols),"start":start.isoformat(),
                                                      "end":end.isoformat(),"limit":50,"sort":"asc","include_content":"true"},"news"):
                        count+=store.put_news_many(page.get("news") or [])
                    store.job("news:"+str(day),"news",day,"done",count)
                except Exception as exc:
                    store.job("news:"+str(day),"news",day,"failed",count,type(exc).__name__+": "+str(exc))
                    if isinstance(exc,PermissionError):raise
        store.meta("last_collection",datetime.now(timezone.utc).isoformat())
        print(json.dumps({"phase":args.phase,"feed":args.feed,"requests":client.calls,"completed_minute_days":store.scalar("SELECT COUNT(*) FROM jobs WHERE job_key LIKE ? AND status='done'",["minute:"+args.feed+":%"])}),flush=True)


def audit(store,days,symbols,feed):
    count_days=store.scalar("SELECT COUNT(*) FROM jobs WHERE job_key LIKE ? AND status='done'",["minute:"+feed+":%"])
    news=store.scalar("SELECT COUNT(*) FROM evidence WHERE kind='news'")
    filings=store.scalar("SELECT COUNT(*) FROM evidence WHERE kind='sec_filing'")
    facts=store.scalar("SELECT COUNT(*) FROM fundamentals")
    # Coverage is refreshed per day for the selected feed. Ignore rows left by a
    # previous pilot of another feed until this feed has completed that day.
    coverage_rows=store.db.execute("""SELECT COALESCE(SUM(c.regular_rows),0), COALESCE(SUM(c.missing_regular),0)
        FROM coverage c JOIN jobs j ON j.day=c.day AND j.status='done'
        WHERE j.job_key='minute:' || ? || ':' || CAST(c.day AS VARCHAR)""",[feed]).fetchone()
    regular,missing=coverage_rows
    daily=store.scalar("SELECT COUNT(*) FROM bars WHERE timeframe='1Day' AND feed=?",[feed])
    minute=store.scalar("SELECT COUNT(*) FROM bars WHERE timeframe='1Min' AND feed=?",[feed])
    first=days[0][0]
    package=get_exam_snapshot(first,datetime.combine(first,datetime.min.time(),ET).replace(hour=9),symbols,_store=store)
    report={"exam_days":len(days),"minute_days_done":count_days,"trade_symbols":len(symbols),"feed":feed,"market_reference":"QQQ Nasdaq-100 ETF proxy",
            "daily_rows":daily,"minute_rows":minute,"regular_rows":regular,"missing_regular_minutes":missing,
            "alpaca_news":news,"sec_filings":filings,"sec_facts":facts,
            "historically_archived_original_news_versions":0,
            "sample_day":str(first),"sample_visible_bars":len(package["bars"]),"sample_visible_evidence":len(package["evidence"]),
            "failed_jobs":store.db.execute("SELECT job_key,error FROM jobs WHERE status='failed'").fetchall()}
    print(json.dumps(report,ensure_ascii=False,default=str))


if __name__=="__main__":main()
