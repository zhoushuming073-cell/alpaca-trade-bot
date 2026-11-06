"""DuckDB exam ledger with historical availability enforced at query time."""
from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb
import pandas as pd
import pandas_market_calendars as mcal

ET = ZoneInfo("America/New_York")
UTC = timezone.utc
CAL = mcal.get_calendar("NYSE")


def sessions(now=None, count=60):
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("Timezone required")
    day = now.astimezone(ET).date()
    schedule = CAL.schedule(start_date=day-timedelta(days=150), end_date=day)
    complete = [(d.date(), r.market_open.to_pydatetime(), r.market_close.to_pydatetime())
                for d, r in schedule.iterrows() if r.market_close.to_pydatetime()+timedelta(minutes=15) <= now]
    if len(complete) < count:
        raise ValueError("Insufficient completed NYSE sessions")
    return complete[-count:]


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


class ExamStore:
    def __init__(self, path, read_only=False):
        self.path = Path(path)
        if not read_only:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = duckdb.connect(str(self.path), read_only=read_only)
        if not read_only:
            try:
                frozen=self.db.execute("SELECT value FROM meta WHERE key='freeze_version'").fetchone()
            except duckdb.CatalogException:
                frozen=None  # New database has no meta table yet.
            if frozen:
                self.db.close()
                raise RuntimeError("Exam database is frozen; create a new version before writing")
            self.db.execute("""
            CREATE TABLE IF NOT EXISTS meta(key VARCHAR PRIMARY KEY, value VARCHAR);
            CREATE TABLE IF NOT EXISTS symbols(symbol VARCHAR PRIMARY KEY, role VARCHAR, label VARCHAR);
            CREATE TABLE IF NOT EXISTS days(day DATE PRIMARY KEY, market_open TIMESTAMPTZ, market_close TIMESTAMPTZ, decision_at TIMESTAMPTZ);
            CREATE TABLE IF NOT EXISTS bars(symbol VARCHAR, timeframe VARCHAR, ts TIMESTAMPTZ, end_at TIMESTAMPTZ,
              available_at TIMESTAMPTZ, day DATE, open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE,
              volume BIGINT, trade_count BIGINT, vwap DOUBLE, source VARCHAR, feed VARCHAR, adjustment VARCHAR,
              fetched_at TIMESTAMPTZ, historical_version VARCHAR, content_hash VARCHAR,
              PRIMARY KEY(symbol,timeframe,ts,feed,adjustment));
            CREATE TABLE IF NOT EXISTS evidence(id VARCHAR PRIMARY KEY, symbols VARCHAR, kind VARCHAR, source VARCHAR,
              url VARCHAR, title VARCHAR, body VARCHAR, first_published TIMESTAMPTZ, updated_at TIMESTAMPTZ,
              fetched_at TIMESTAMPTZ, content_hash VARCHAR, historical_status VARCHAR, status_reason VARCHAR);
            CREATE TABLE IF NOT EXISTS fundamentals(symbol VARCHAR, taxonomy VARCHAR, tag VARCHAR, unit VARCHAR,
              period_start DATE, period_end DATE, form VARCHAR, accession VARCHAR, available_at TIMESTAMPTZ,
              value DOUBLE, source_url VARCHAR, fetched_at TIMESTAMPTZ, historical_status VARCHAR,
              content_hash VARCHAR, PRIMARY KEY(symbol,taxonomy,tag,unit,period_start,period_end,accession,value));
            CREATE TABLE IF NOT EXISTS jobs(job_key VARCHAR PRIMARY KEY, kind VARCHAR, day DATE, status VARCHAR,
              row_count BIGINT, attempts INTEGER, error VARCHAR, updated_at TIMESTAMPTZ);
            CREATE TABLE IF NOT EXISTS coverage(day DATE, symbol VARCHAR, regular_rows INTEGER, premarket_rows INTEGER,
              missing_regular INTEGER, status VARCHAR, PRIMARY KEY(day,symbol));
            CREATE TABLE IF NOT EXISTS audit(key VARCHAR PRIMARY KEY, value VARCHAR);
            CREATE OR REPLACE VIEW weekly_bars AS
              SELECT symbol, date_trunc('week', ts AT TIME ZONE 'America/New_York') AS period,
                first(open ORDER BY ts) AS open, max(high) AS high, min(low) AS low,
                last(close ORDER BY ts) AS close, sum(volume) AS volume,
                max(end_at) AS end_at, max(available_at) AS available_at, feed, adjustment
              FROM bars WHERE timeframe='1Day' GROUP BY symbol, period, feed, adjustment;
            CREATE OR REPLACE VIEW monthly_bars AS
              SELECT symbol, date_trunc('month', ts AT TIME ZONE 'America/New_York') AS period,
                first(open ORDER BY ts) AS open, max(high) AS high, min(low) AS low,
                last(close ORDER BY ts) AS close, sum(volume) AS volume,
                max(end_at) AS end_at, max(available_at) AS available_at, feed, adjustment
              FROM bars WHERE timeframe='1Day' GROUP BY symbol, period, feed, adjustment;
            """)

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def scalar(self, sql, args=()):
        return self.db.execute(sql, args).fetchone()[0]

    def meta(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", [key, str(value)])

    def job_done(self, key):
        r = self.db.execute("SELECT status FROM jobs WHERE job_key=?", [key]).fetchone()
        return bool(r and r[0] == "done")

    def job(self, key, kind, day, status, rows=0, error=""):
        prior = self.db.execute("SELECT attempts FROM jobs WHERE job_key=?", [key]).fetchone()
        self.db.execute("INSERT OR REPLACE INTO jobs VALUES (?,?,?,?,?,?,?,?)",
                        [key, kind, day, status, rows, (prior[0] if prior else 0)+1, error[:300], datetime.now(UTC)])

    def bar_values(self, symbol, timeframe, item, *, source="Alpaca", feed="iex", adjustment="raw"):
        ts = datetime.fromisoformat(item["t"].replace("Z", "+00:00"))
        if ts.tzinfo is None:
            raise ValueError("Untimed bar")
        ts = ts.astimezone(UTC)
        if timeframe == "1Min":
            end_at = ts + timedelta(minutes=1)
            available = end_at + timedelta(seconds=30)
        elif timeframe == "1Day":
            local = ts.astimezone(ET)
            schedule = CAL.schedule(start_date=local.date(), end_date=local.date())
            if schedule.empty:
                raise ValueError("Daily bar on closed session")
            end_at = schedule.iloc[0].market_close.to_pydatetime()
            available = end_at + timedelta(minutes=15)
        else:
            raise ValueError("Unsupported source timeframe")
        prices = [float(item[k]) for k in ("o", "h", "l", "c")]
        if min(prices) <= 0 or prices[1] < max(prices[0],prices[3]) or prices[2] > min(prices[0],prices[3]):
            raise ValueError("Invalid OHLC")
        volume = int(item["v"])
        if volume < 0:
            raise ValueError("Negative volume")
        return [symbol,timeframe,ts,end_at,available,ts.astimezone(ET).date(),*prices,volume,
                int(item.get("n") or 0),float(item.get("vw") or 0),source,feed,adjustment,
                datetime.now(UTC),"provider_reconstructed",digest(item)]

    def put_bar(self, symbol, timeframe, item, **kwargs):
        self.db.execute("INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        self.bar_values(symbol,timeframe,item,**kwargs))

    def put_bars(self, items, timeframe, *, feed="iex"):
        rows=[self.bar_values(symbol,timeframe,bar,feed=feed) for symbol,bar in items]
        if not rows:return 0
        columns=[d[1] for d in self.db.execute("PRAGMA table_info('bars')").fetchall()]
        frame=pd.DataFrame(rows,columns=columns)
        self.db.register("exam_batch",frame)
        try:
            self.db.execute("INSERT OR REPLACE INTO bars SELECT * FROM exam_batch")
        finally:
            self.db.unregister("exam_batch")
        return len(rows)

    def put_news(self, item):
        self.db.execute("INSERT OR REPLACE INTO evidence VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",self.news_values(item))

    def news_values(self, item):
        first = datetime.fromisoformat(item["created_at"].replace("Z", "+00:00"))
        updated = datetime.fromisoformat(item.get("updated_at", item["created_at"]).replace("Z", "+00:00"))
        if first.tzinfo is None or updated.tzinfo is None:
            raise ValueError("Untimed news")
        title = item.get("headline") or ""
        body = item.get("content") or item.get("summary") or ""
        symbols = item.get("symbols") or []
        raw = {"title":title,"body":body,"updated_at":updated.isoformat(),"url":item.get("url")}
        return ["alpaca:"+str(item["id"]), json.dumps(symbols),"news",item.get("source") or "Alpaca News",
                item.get("url"),title,body,first,updated,datetime.now(UTC),digest(raw),
                "provider_timestamp_version_unknown","Historical article may have been revised after publication"]

    def put_news_many(self, items):
        rows=[self.news_values(item) for item in items]
        if not rows:return 0
        columns=[d[1] for d in self.db.execute("PRAGMA table_info('evidence')").fetchall()]
        frame=pd.DataFrame(rows,columns=columns).drop_duplicates(subset=["id"],keep="last")
        self.db.register("exam_news",frame)
        try:
            self.db.execute("INSERT OR REPLACE INTO evidence SELECT * FROM exam_news")
        finally:
            self.db.unregister("exam_news")
        return len(rows)

    def put_sec_filing(self, symbol, cik, form, accession, primary_document, accepted):
        if accepted.tzinfo is None:
            raise ValueError("SEC acceptance timestamp requires timezone")
        archive_accession=accession.replace("-","")
        url=f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{archive_accession}/{primary_document}"
        source={"symbol":symbol,"cik":int(cik),"form":form,"accession":accession,
                "primary_document":primary_document,"accepted":accepted.isoformat()}
        self.db.execute("INSERT OR REPLACE INTO evidence VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        ["sec:"+accession+":"+symbol,json.dumps([symbol]),"sec_filing","SEC EDGAR",url,
                         f"{symbol} {form} filed with SEC",json.dumps(source,sort_keys=True),accepted,accepted,
                         datetime.now(UTC),digest(source),"sec_acceptance_timestamp_verified",
                         "Official acceptance time; filing document was fetched later and may have post-acceptance corrections"])

    def put_fundamentals(self, rows):
        if not rows:return 0
        columns=[d[1] for d in self.db.execute("PRAGMA table_info('fundamentals')").fetchall()]
        frame=pd.DataFrame(rows,columns=columns)
        frame=frame.drop_duplicates(subset=["symbol","taxonomy","tag","unit","period_start","period_end","accession","value"])
        self.db.register("exam_facts",frame)
        try:
            self.db.execute("INSERT OR REPLACE INTO fundamentals SELECT * FROM exam_facts")
        finally:
            self.db.unregister("exam_facts")
        return len(rows)

    def coverage(self, day, symbols, *, feed="iex"):
        schedule = CAL.schedule(start_date=day,end_date=day)
        if schedule.empty:
            raise ValueError("Not an NYSE day")
        opening,close = schedule.iloc[0][["market_open","market_close"]]
        pre = datetime.combine(day,time(4),ET).astimezone(UTC)
        expected = int((close-opening).total_seconds()//60)
        for symbol in symbols:
            regular = self.scalar("SELECT COUNT(*) FROM bars WHERE symbol=? AND timeframe='1Min' AND feed=? AND ts>=? AND ts<?",[symbol,feed,opening,close])
            morning = self.scalar("SELECT COUNT(*) FROM bars WHERE symbol=? AND timeframe='1Min' AND feed=? AND ts>=? AND ts<?",[symbol,feed,pre,opening])
            status = "complete" if regular==expected else "missing_minutes_or_unavailable" if regular else "no_regular_bars"
            self.db.execute("INSERT OR REPLACE INTO coverage VALUES (?,?,?,?,?,?)",
                            [day,symbol,regular,morning,max(0,expected-regular),status])


def get_exam_snapshot(date_value, asof_time, symbols, *, db_path=None, _store=None):
    """Return only records that could have completed by the requested ET instant."""
    day = date.fromisoformat(str(date_value))
    asof = datetime.fromisoformat(str(asof_time).replace("Z","+00:00")) if not isinstance(asof_time,datetime) else asof_time
    if asof.tzinfo is None or asof.astimezone(ET).date()!=day:
        raise ValueError("asof must be timezone-aware and on the requested ET day")
    db_path = db_path or Path(__file__).resolve().parents[2] / "data/exam_data.duckdb"
    with nullcontext(_store) if _store is not None else ExamStore(db_path, read_only=True) as store:
        known = dict(store.db.execute("SELECT symbol,role FROM symbols").fetchall())
        requested = list(dict.fromkeys(symbols))
        if not requested or any(known.get(s)!="trade" for s in requested):
            raise ValueError("Snapshot symbols must be in the fixed trade pool")
        universe = requested+[s for s,r in known.items() if r=="market_reference"]
        chosen_feed=store.db.execute("SELECT value FROM meta WHERE key='feed'").fetchone()
        feed=chosen_feed[0] if chosen_feed else "iex"
        cutoff = asof.astimezone(UTC)
        marks = ",".join("?" for _ in universe)
        rows = store.db.execute(
            f"SELECT symbol,timeframe,ts,end_at,available_at,open,high,low,close,volume,source,feed,adjustment,historical_version "
            f"FROM bars WHERE symbol IN ({marks}) AND feed=? AND available_at<=? AND ts<? "
            f"AND (timeframe='1Day' OR (timeframe='1Min' AND day=?)) ORDER BY symbol,ts",
            [*universe,feed,cutoff,cutoff,day]).fetchall()
        cols = [d[0] for d in store.db.description]
        bars = [dict(zip(cols,r)) for r in rows]
        # Aggregate only the already visible daily bars. A materialized full-week
        # or full-month candle would leak later closes into an earlier snapshot.
        aggregates={"weekly":[],"monthly":[]}
        for frequency in aggregates:
            grouped={}
            for bar in bars:
                if bar["timeframe"]!="1Day":continue
                local_day=bar["ts"].astimezone(ET).date()
                period=(local_day-timedelta(days=local_day.weekday())) if frequency=="weekly" else local_day.replace(day=1)
                key=(bar["symbol"],period)
                if key not in grouped:
                    grouped[key]={"symbol":bar["symbol"],"period":period,"open":bar["open"],"high":bar["high"],
                                  "low":bar["low"],"close":bar["close"],"volume":bar["volume"],
                                  "end_at":bar["end_at"],"available_at":bar["available_at"],
                                  "feed":feed,"adjustment":bar["adjustment"]}
                else:
                    item=grouped[key]
                    item["high"]=max(item["high"],bar["high"])
                    item["low"]=min(item["low"],bar["low"])
                    item["close"]=bar["close"]
                    item["volume"]+=bar["volume"]
                    item["end_at"]=bar["end_at"]
                    item["available_at"]=bar["available_at"]
            aggregates[frequency]=list(grouped.values())
        articles = store.db.execute(
            "SELECT id,symbols,kind,source,url,title,body,first_published,updated_at,content_hash,historical_status,status_reason "
            "FROM evidence WHERE first_published<=? AND updated_at<=? ORDER BY first_published",[cutoff,cutoff]).fetchall()
        ecols = [d[0] for d in store.db.description]
        news = []
        for row in articles:
            item=dict(zip(ecols,row))
            related=json.loads(item["symbols"])
            if related and not set(related).intersection(universe):
                continue
            item["symbols"]=related
            news.append(item)
        facts=store.db.execute(
            f"SELECT symbol,taxonomy,tag,unit,period_start,period_end,form,accession,available_at,value,source_url,historical_status "
            f"FROM fundamentals WHERE symbol IN ({marks}) AND available_at<=? ORDER BY symbol,tag,available_at",
            [*universe,cutoff]).fetchall()
        fcols=[d[0] for d in store.db.description]
        decision_at=datetime.combine(day,time(9),ET).astimezone(UTC)
        return {"date":day.isoformat(),"asof":cutoff.isoformat(),"reference_decision_at":decision_at.isoformat(),
                "symbols":requested,"market_reference":[s for s in universe if known.get(s)=="market_reference"],
                "bars":bars,"weekly_bars":aggregates["weekly"],"monthly_bars":aggregates["monthly"],
                "evidence":news,"fundamentals":[dict(zip(fcols,r)) for r in facts],"historical_truth":{
                  "bars":"provider timestamps; content reconstructed later and may include corrections",
                  "news":"provider publication and update timestamps; original article version not independently archived",
                  "fundamentals":"SEC accession acceptance timestamps; present-day API can include later corrections"}}
