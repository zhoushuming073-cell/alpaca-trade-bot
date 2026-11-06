"""Read-only 60-day point-in-time audit with future injection in an isolated copy."""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import duckdb
import requests
import yaml

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from exam_data import ExamStore, get_exam_snapshot  # noqa:E402
from exam_data.store import ET  # noqa:E402

DB=ROOT/"data/exam_data.duckdb"
OUT=ROOT/"data/lookahead_audit"
SAMPLES={date(2026,8,27):"major_news_or_earnings",
         date(2026,9,25):"ordinary_session",
         date(2026,11,5):"high_volatility"}


def as_text(value):
    if isinstance(value,datetime):return value.astimezone(timezone.utc).isoformat()
    if isinstance(value,date):return value.isoformat()
    raise TypeError(type(value).__name__)


def encoded(packet):
    return json.dumps(packet,ensure_ascii=False,default=as_text,sort_keys=True,separators=(",",":")).encode("utf-8")


def fingerprint(packet):
    return hashlib.sha256(encoded(packet)).hexdigest()


def inspect_packet(packet,cutoff):
    """Count explicit availability or aggregation violations, retaining examples."""
    errors=[]
    counters=Counter()
    day=date.fromisoformat(packet["date"])
    visible_daily={}
    for bar in packet["bars"]:
        counters["daily" if bar["timeframe"]=="1Day" else "minute"]+=1
        if bar["available_at"]>cutoff or bar["end_at"]>cutoff or bar["ts"]>=cutoff:
            errors.append(("future_bar",bar["symbol"],bar["timeframe"],as_text(bar["ts"])))
        if bar["timeframe"]=="1Min":
            if bar["ts"].astimezone(ET).date()!=day or bar["end_at"]!=bar["ts"]+timedelta(minutes=1):
                errors.append(("minute_window",bar["symbol"],as_text(bar["ts"])))
        elif bar["timeframe"]=="1Day":
            if bar["ts"].astimezone(ET).date()>=day:
                errors.append(("unfinished_daily",bar["symbol"],as_text(bar["ts"])))
            visible_daily.setdefault(bar["symbol"],[]).append(bar)
        else:errors.append(("unknown_timeframe",bar["symbol"],bar["timeframe"]))
    for frequency,key in (("weekly","weekly_bars"),("monthly","monthly_bars")):
        expected={}
        for symbol,rows in visible_daily.items():
            for bar in rows:
                local=bar["ts"].astimezone(ET).date()
                period=local-timedelta(days=local.weekday()) if frequency=="weekly" else local.replace(day=1)
                item=expected.get((symbol,period))
                if item is None:
                    expected[(symbol,period)]={"open":bar["open"],"high":bar["high"],"low":bar["low"],
                                              "close":bar["close"],"volume":bar["volume"],"end_at":bar["end_at"],
                                              "available_at":bar["available_at"]}
                else:
                    item["high"]=max(item["high"],bar["high"])
                    item["low"]=min(item["low"],bar["low"])
                    item["close"]=bar["close"]
                    item["volume"]+=bar["volume"]
                    item["end_at"]=bar["end_at"]
                    item["available_at"]=bar["available_at"]
        seen=set()
        for item in packet[key]:
            counters[frequency]+=1
            group=(item["symbol"],item["period"])
            seen.add(group)
            if item["available_at"]>cutoff or item["end_at"]>cutoff:
                errors.append(("future_aggregate",frequency,item["symbol"],str(item["period"])))
            if expected.get(group)!= {name:item[name] for name in ("open","high","low","close","volume","end_at","available_at")}:
                errors.append(("aggregate_mismatch",frequency,item["symbol"],str(item["period"])))
        if seen!=set(expected):errors.append(("aggregate_set_mismatch",frequency,len(seen),len(expected)))
    for item in packet["evidence"]:
        counters[item["kind"]]+=1
        if item["first_published"]>cutoff or item["updated_at"]>cutoff:
            errors.append(("future_evidence",item["id"],as_text(item["first_published"])))
    for item in packet["fundamentals"]:
        counters["fundamentals"]+=1
        if item["available_at"]>cutoff:
            errors.append(("future_fundamental",item["symbol"],item["accession"]))
        if item["period_end"]>day:
            errors.append(("future_fiscal_period",item["symbol"],item["accession"]))
    forbidden={"future_returns","outcomes","orders","fills","pnl","realized_returns","trade_results"}
    if forbidden.intersection(packet):errors.append(("future_result_field",sorted(forbidden.intersection(packet))))
    return counters,errors


def sample_export(store,packet,output_dir):
    day=packet["date"]
    prefix=output_dir/f"{day}_0900"
    prefix.with_suffix(".packet.json").write_text(json.dumps(packet,ensure_ascii=False,default=as_text,indent=2),encoding="utf-8")
    cutoff=datetime.fromisoformat(packet["asof"])
    rows=store.db.execute("""SELECT symbol,timeframe,ts,fetched_at,content_hash,source,feed,adjustment,
        historical_version FROM bars WHERE feed='sip' AND available_at<=? AND ts<?
        AND (timeframe='1Day' OR (timeframe='1Min' AND day=?))""",
        [cutoff,cutoff,date.fromisoformat(day)]).fetchall()
    bar_provenance={(r[0],r[1],as_text(r[2])):r for r in rows}
    evidence_provenance={r[0]:r for r in store.db.execute(
        "SELECT id,fetched_at,content_hash,source,url,historical_status FROM evidence").fetchall()}
    fact_provenance={(r[0],r[1],r[2],r[3],str(r[4]),str(r[5]),r[6],r[7]):r for r in store.db.execute(
        "SELECT symbol,taxonomy,tag,unit,period_start,period_end,accession,value,fetched_at,content_hash,source_url,historical_status FROM fundamentals").fetchall()}
    ledger=prefix.with_suffix(".provenance.jsonl")
    with ledger.open("w",encoding="utf-8") as handle:
        def emit(record):handle.write(json.dumps(record,ensure_ascii=False,default=as_text)+"\n")
        for bar in packet["bars"]:
            r=bar_provenance[(bar["symbol"],bar["timeframe"],as_text(bar["ts"]))]
            emit({"kind":"bar","symbol":bar["symbol"],"timeframe":bar["timeframe"],"source_timestamp":bar["ts"],
                  "end_at":bar["end_at"],"available_at_assumed":bar["available_at"],"retrieved_at":r[3],
                  "content_hash":r[4],"source":r[5],"feed":r[6],"adjustment":r[7],"status":r[8]})
        for frequency,key in (("weekly","weekly_bars"),("monthly","monthly_bars")):
            for bar in packet[key]:
                emit({"kind":frequency,"symbol":bar["symbol"],"period":bar["period"],
                      "end_at":bar["end_at"],"available_at_assumed":bar["available_at"],
                      "source":"derived from snapshot-visible daily bars","status":"derived_reconstructed"})
        for item in packet["evidence"]:
            r=evidence_provenance[item["id"]]
            emit({"kind":item["kind"],"id":item["id"],"symbols":item["symbols"],"source":r[3],"url":r[4],
                  "created_at":item["first_published"],"updated_at":item["updated_at"],"retrieved_at":r[1],
                  "content_hash":r[2],"status":r[5]})
        for item in packet["fundamentals"]:
            k=(item["symbol"],item["taxonomy"],item["tag"],item["unit"],str(item["period_start"]),
               str(item["period_end"]),item["accession"],item["value"])
            r=fact_provenance[k]
            emit({"kind":"fundamental","symbol":item["symbol"],"tag":item["tag"],"accession":item["accession"],
                  "period_start":item["period_start"],"period_end":item["period_end"],"available_at":item["available_at"],
                  "retrieved_at":r[8],"content_hash":r[9],"source_url":r[10],"status":r[11]})
    return prefix.with_suffix(".packet.json"),ledger


def inject_future(db,day):
    for hour,minute in ((8,59),(9,0)):
        ts=datetime.combine(day,time(hour,minute),ET).astimezone(timezone.utc)
        db.execute("""INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ["AMD","1Min",ts,ts+timedelta(minutes=1),ts+timedelta(minutes=1,seconds=30),day,
             999,1000,998,999,1,1,999,"AUDIT_FUTURE_INJECTION","sip","raw",
             datetime.now(timezone.utc),"injected_future","audit"])
    morning=datetime.combine(day,time(9),ET).astimezone(timezone.utc)
    db.execute("INSERT OR REPLACE INTO evidence VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ["audit-news:"+str(day),json.dumps(["AMD"]),"news","AUDIT_FUTURE_INJECTION",None,
         "Future injected news","audit only",morning+timedelta(seconds=1),morning+timedelta(seconds=1),
         datetime.now(timezone.utc),"audit","injected_future","must stay hidden"])
    db.execute("INSERT INTO fundamentals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ["AMD","audit","FutureReturnLabel","ratio",day,day,"AUDIT","audit:"+str(day),
         morning+timedelta(seconds=1),123.0,"audit-only",datetime.now(timezone.utc),"injected_future","audit"])
    db.execute("INSERT INTO future_returns VALUES (?,?,?)",[day,"AMD",123.0])


def main():
    OUT.mkdir(parents=True,exist_ok=True)
    symbols=yaml.safe_load((ROOT/"config/watchlist.yaml").read_text(encoding="utf-8"))["symbols"]
    manifest=json.loads((ROOT/"data/exam_data.manifest.json").read_text(encoding="utf-8"))
    with DB.open("rb") as handle:before_db_hash=hashlib.file_digest(handle,"sha256").hexdigest()
    if before_db_hash!=manifest["db_sha256"]:raise RuntimeError("Frozen database hash mismatch before audit")
    counters=Counter();errors=[];baseline={};sample_files={};day_rows=[]
    with ExamStore(DB,read_only=True) as store:
        days=[r[0] for r in store.db.execute("SELECT day FROM days ORDER BY day").fetchall()]
        if len(days)!=60 or len(symbols)!=48:raise RuntimeError("Unexpected exam scope")
        for day in days:
            cutoff=datetime.combine(day,time(9),ET).astimezone(timezone.utc)
            packet=get_exam_snapshot(day,cutoff,symbols,_store=store)
            counts,problems=inspect_packet(packet,cutoff)
            counters.update(counts)
            errors.extend((day.isoformat(),*problem) for problem in problems)
            baseline[day.isoformat()]=fingerprint(packet)
            day_rows.append({"date":day.isoformat(),"asof":cutoff.isoformat(),"hash":baseline[day.isoformat()],
                             "counts":dict(counts),"violations":len(problems)})
            if day in SAMPLES:
                a,b=sample_export(store,packet,OUT)
                sample_files[day.isoformat()]={"type":SAMPLES[day],"packet":str(a.relative_to(ROOT)).replace("\\","/"),
                                               "provenance":str(b.relative_to(ROOT)).replace("\\","/")}
            if len(day_rows)%10==0:print(json.dumps({"snapshots_checked":len(day_rows),"violations":len(errors)}),flush=True)
        sql_checks={
          "news_total":store.scalar("SELECT COUNT(*) FROM evidence WHERE kind='news'"),
          "news_missing_source_or_time":store.scalar("SELECT COUNT(*) FROM evidence WHERE kind='news' AND (source IS NULL OR source='' OR first_published IS NULL OR updated_at IS NULL OR fetched_at IS NULL)"),
          "news_updated_after_retrieval":store.scalar("SELECT COUNT(*) FROM evidence WHERE kind='news' AND updated_at>fetched_at"),
          "news_original_version_archived":store.scalar("SELECT COUNT(*) FROM evidence WHERE kind='news' AND historical_status='historical_version_archived'"),
          "sec_filings":store.scalar("SELECT COUNT(*) FROM evidence WHERE kind='sec_filing'"),
          "fundamentals":store.scalar("SELECT COUNT(*) FROM fundamentals"),
          "facts_without_stored_filing_row":store.scalar("""SELECT COUNT(*) FROM fundamentals f LEFT JOIN evidence e
              ON e.id='sec:'||f.accession||':'||f.symbol WHERE e.id IS NULL"""),
          "facts_acceptance_mismatch":store.scalar("""SELECT COUNT(*) FROM fundamentals f JOIN evidence e
              ON e.id='sec:'||f.accession||':'||f.symbol WHERE f.available_at<>e.first_published"""),
          "bars_with_unknown_version":store.scalar("SELECT COUNT(*) FROM bars WHERE feed='sip' AND historical_version='provider_reconstructed'"),
          "zero_regular_bar_symbol_days":store.scalar("SELECT COUNT(*) FROM coverage WHERE regular_rows=0"),
          "raw_adjustment_rows":store.scalar("SELECT COUNT(*) FROM bars WHERE feed='sip' AND adjustment='raw'"),
          "sip_recent_15min_visible_at_0900":store.scalar("""SELECT COUNT(*) FROM bars b JOIN days d ON b.day=d.day
              WHERE b.feed='sip' AND b.timeframe='1Min' AND b.available_at<=d.decision_at
              AND b.end_at>d.decision_at-INTERVAL 15 MINUTE AND b.end_at<=d.decision_at"""),
        }
        listing=store.db.execute("""SELECT symbol,MIN(day),MAX(day),COUNT(*) FROM bars
            WHERE feed='sip' AND timeframe='1Day' GROUP BY symbol ORDER BY symbol""").fetchall()
        sql_checks["earliest_daily_by_symbol"]={s:str(start) for s,start,_,_ in listing}
        sql_checks["daily_count_by_symbol"]={s:n for s,_,_,n in listing}
    (OUT/"all_60_0900_hashes.json").write_text(json.dumps(day_rows,ensure_ascii=False,indent=2),encoding="utf-8")
    try:
        from trading_bot.dashboard import alpaca_environment
        credentials=alpaca_environment()
        response=requests.get("https://data.alpaca.markets/v2/stocks/AAPL/trades/latest",
            headers={"APCA-API-KEY-ID":credentials["ALPACA_API_KEY"],
                     "APCA-API-SECRET-KEY":credentials["ALPACA_API_SECRET"]},
            params={"feed":"sip"},timeout=(5,15))
        payload=response.json()
        probe={"checked_at":datetime.now(timezone.utc).isoformat(),"method":"GET",
               "endpoint":"https://data.alpaca.markets/v2/stocks/AAPL/trades/latest?feed=sip",
               "http_status":response.status_code,"message":payload.get("message","") if isinstance(payload,dict) else "",
               "note":"Current key entitlement only; historical subscription state was not archived"}
    except Exception as exc:
        probe={"checked_at":datetime.now(timezone.utc).isoformat(),"status":"UNKNOWN",
               "error_type":type(exc).__name__,"note":"No credentials or response body saved"}
    (OUT/"sip_access_probe.json").write_text(json.dumps(probe,ensure_ascii=False,indent=2),encoding="utf-8")
    # Copy the frozen file once, then inject future values for every examined day.
    with tempfile.TemporaryDirectory(prefix="exam_lookahead_") as temporary:
        copy_path=Path(temporary)/"injection.duckdb"
        shutil.copyfile(DB,copy_path)
        injection_errors=[]
        with duckdb.connect(str(copy_path)) as copy_db:
            copy_db.execute("CREATE TABLE future_returns(day DATE,symbol VARCHAR,post_close_return DOUBLE)")
            for i,day in enumerate(days,1):
                cutoff=datetime.combine(day,time(9),ET).astimezone(timezone.utc)
                copy_db.execute("BEGIN TRANSACTION")
                try:
                    inject_future(copy_db,day)
                    packet=get_exam_snapshot(day,cutoff,symbols,_store=SimpleNamespace(db=copy_db))
                    new_hash=fingerprint(packet)
                    if new_hash!=baseline[day.isoformat()]:injection_errors.append(day.isoformat())
                finally:
                    copy_db.execute("ROLLBACK")
                if i%10==0:print(json.dumps({"injection_days_checked":i,"hash_mismatches":len(injection_errors)}),flush=True)
    with DB.open("rb") as handle:after_db_hash=hashlib.file_digest(handle,"sha256").hexdigest()
    if after_db_hash!=before_db_hash:raise RuntimeError("Formal database changed during audit")
    result={"days":len(days),"trade_symbols":len(symbols),"reference":"QQQ","asof":"09:00 America/New_York",
            "snapshot_items":dict(counters),"explicit_violations":len(errors),"violation_examples":errors[:30],
            "injection_hash_mismatches":injection_errors,"sql_checks":sql_checks,"sip_access_probe":probe,
            "sample_files":sample_files,
            "formal_db_sha256_before":before_db_hash,"formal_db_sha256_after":after_db_hash}
    (OUT/"audit_results.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({"days":len(days),"explicit_violations":len(errors),"hash_mismatches":len(injection_errors),
                      "snapshot_items":dict(counters),"sql_checks":sql_checks,
                      "formal_db_unchanged":before_db_hash==after_db_hash},ensure_ascii=False),flush=True)


if __name__=="__main__":main()
