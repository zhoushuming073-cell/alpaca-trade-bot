"""Read-only V2 audit; future injection occurs only in a disposable copied database."""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import duckdb
import yaml

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from exam_data import ExamStore  # noqa:E402
from exam_data.store import ET  # noqa:E402
from exam_v2.prompts import build_exam_prompt  # noqa:E402
from exam_v2.snapshot import ExamMode,get_v2_snapshot  # noqa:E402

V1=ROOT/"data/exam_data.duckdb"
V2=ROOT/"data/exam_v2.duckdb"
OUT=ROOT/"data/exam_v2_audit"


def canon(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,default=str,separators=(",",":"))


def digest(value):return hashlib.sha256(canon(value).encode()).hexdigest()


def sha(path):
    with path.open("rb") as handle:return hashlib.file_digest(handle,"sha256").hexdigest()


def check(packet,cutoff,mode):
    failures=[]
    day=date.fromisoformat(packet["date"])
    for b in packet["bars"]:
        if b["feed"]!="iex" or b["available_at"]>cutoff or b["end_at"]>cutoff:
            failures.append(("bar_availability",b["symbol"],str(b["ts"])))
        if b["symbol"]=="SPCX" and b["ts"].astimezone(ET).date()<date(2026,6,12):
            failures.append(("old_spcx",str(b["ts"])))
        if b["timeframe"]=="1Day" and b["ts"].astimezone(ET).date()>=day:
            failures.append(("unfinished_daily",b["symbol"],str(b["ts"])))
        if b["timeframe"]=="1Min":
            embargo=timedelta(minutes=15) if mode is ExamMode.STRICT else timedelta(seconds=30)
            if b["end_at"]>cutoff-embargo or b["ts"].astimezone(ET).date()!=day:
                failures.append(("minute_window",b["symbol"],str(b["ts"])))
    visible={(b["symbol"],b["ts"]):b for b in packet["bars"] if b["timeframe"]=="1Day"}
    for freq in ("weekly","monthly"):
        for b in packet[freq+"_bars"]:
            if b["end_at"]>cutoff or b["available_at"]>cutoff:
                failures.append(("future_aggregate",freq,b["symbol"]))
            if not any(s==b["symbol"] and x["end_at"]==b["end_at"] for (s,_),x in visible.items()):
                failures.append(("aggregate_not_from_visible_daily",freq,b["symbol"]))
    for e in packet["evidence"]:
        if e["first_published"]>cutoff or e.get("updated_at",e["first_published"])>cutoff:
            failures.append(("future_evidence",e["id"]))
        if mode is ExamMode.STRICT and (e["kind"]!="sec_filing_metadata" or "body" in e):
            failures.append(("unknown_content_in_strict",e["id"]))
        if mode is ExamMode.EXPLORATORY and not e["historical_status"].startswith("UNKNOWN"):
            failures.append(("unlabeled_exploratory",e["id"]))
    for f in packet["fundamentals"]:
        if f["available_at"]>cutoff or mode is ExamMode.STRICT:
            failures.append(("future_or_unknown_fact",f["symbol"]))
    forbidden={"future_returns","outcomes","orders","fills","pnl","trade_results"}
    if forbidden.intersection(packet):failures.append(("future_result_field",))
    return failures


def injected_bar(at):
    return {"t":at.astimezone(timezone.utc).isoformat(),"o":999,"h":1000,"l":998,"c":999,"v":100}


def audit_account(cutoff):
    """An explicit empty simulator state for input isolation checks only."""
    return {"source":"ISOLATED_SIMULATOR","asof_utc":cutoff.isoformat(),
            "cash":100000.0,"equity":100000.0,"positions":{},"open_orders":[],
            "historical_short_locates":"UNKNOWN"}


def main():
    OUT.mkdir(parents=True,exist_ok=True)
    v1_before=sha(V1);v2_before=sha(V2)
    manifest=json.loads((ROOT/"data/exam_data.manifest.json").read_text(encoding="utf-8"))
    if v1_before!=manifest["db_sha256"]:raise RuntimeError("Frozen V1 changed")
    symbols=yaml.safe_load((ROOT/"config/watchlist.yaml").read_text(encoding="utf-8"))["symbols"]
    with duckdb.connect(str(V2),read_only=True) as db:
        days=[r[0] for r in db.execute("SELECT day FROM days ORDER BY day").fetchall()]
        if len(days)!=60:raise RuntimeError("V2 must contain 60 NYSE days")
        raw_counts={"daily":db.execute("SELECT COUNT(*) FROM bars WHERE timeframe='1Day'").fetchone()[0],
                    "minute":db.execute("SELECT COUNT(*) FROM bars WHERE timeframe='1Min'").fetchone()[0],
                    "premarket":db.execute("SELECT SUM(premarket_rows) FROM coverage").fetchone()[0],
                    "regular_missing":db.execute("SELECT SUM(missing_regular) FROM coverage").fetchone()[0],
                    "zero_regular_symbol_days":db.execute("SELECT COUNT(*) FROM coverage WHERE regular_rows=0").fetchone()[0],
                    "news":db.execute("SELECT COUNT(*) FROM evidence WHERE kind='news'").fetchone()[0],
                    "filings":db.execute("SELECT COUNT(*) FROM evidence WHERE kind='sec_filing'").fetchone()[0],
                    "facts":db.execute("SELECT COUNT(*) FROM fundamentals").fetchone()[0],
                    "old_spcx":db.execute("SELECT COUNT(*) FROM bars WHERE symbol='SPCX' AND day<DATE '2026-06-12'").fetchone()[0],
                    "non_iex":db.execute("SELECT COUNT(*) FROM bars WHERE feed<>'iex'").fetchone()[0]}
        corporate=db.execute("SELECT kind,COUNT(*) FROM corporate_action_audit GROUP BY kind ORDER BY kind").fetchall()
        identity=db.execute("SELECT identity_status,COUNT(*) FROM security_identity GROUP BY 1").fetchall()
    counts={mode.value:Counter() for mode in ExamMode}
    originals={};prompt_hashes={};errors=[];day_summary=[]
    for day_index,day in enumerate(days,1):
        cut=datetime.combine(day,time(9),ET)
        row={"day":str(day)}
        for mode in ExamMode:
            packet=get_v2_snapshot(day,cut,symbols,mode=mode,db_path=V2)
            prompt=build_exam_prompt(day,cut,symbols,mode=mode,db_path=V2,
                                     simulated_account=audit_account(cut))
            failures=check(packet,cut,mode)
            errors.extend((str(day),mode.value,*x) for x in failures)
            originals[(day,mode.value)]=digest(packet)
            prompt_hashes[(day,mode.value)]=prompt.input_sha256
            c=counts[mode.value]
            c["snapshots"]+=1;c["daily_visible"]+=sum(b["timeframe"]=="1Day" for b in packet["bars"])
            c["minute_visible"]+=sum(b["timeframe"]=="1Min" for b in packet["bars"])
            c["week_visible"]+=len(packet["weekly_bars"]);c["month_visible"]+=len(packet["monthly_bars"])
            c["news_visible"]+=sum(e["kind"]=="news" for e in packet["evidence"])
            c["sec_metadata_visible"]+=sum(e["kind"]=="sec_filing_metadata" for e in packet["evidence"])
            c["facts_visible"]+=len(packet["fundamentals"])
            c["availability_dropped"]+=packet["dropped_rows"]["availability"]
            c["identity_dropped"]+=packet["dropped_rows"]["identity"]
            c["symbol_days_with_prior_daily"]+=48-len(packet["unavailable_symbols"])
            row[mode.value]={"snapshot_sha256":originals[(day,mode.value)],
                             "prompt_sha256":prompt_hashes[(day,mode.value)],
                             "minute_visible":sum(b["timeframe"]=="1Min" for b in packet["bars"]),
                             "unavailable_symbols":packet["unavailable_symbols"]}
        day_summary.append(row)
        if day_index%10==0:print(f"V2 09:00 audit {day_index}/60",flush=True)
    # Future data is injected into a copy and rolled back for each date. It cannot
    # alter a later day's baseline or write to the frozen source databases.
    mismatches=[];intraday_failures=[]
    with tempfile.TemporaryDirectory(prefix="exam-v2-inject-") as scratch:
        copied=Path(scratch)/"inject.duckdb"
        shutil.copy2(V2,copied)
        with duckdb.connect(str(copied)) as copied_db:
            copied_db.execute("DELETE FROM meta WHERE key IN ('freeze_version','freeze_at')")
        with ExamStore(copied) as store:
            for day_index,day in enumerate(days,1):
                cut=datetime.combine(day,time(9),ET)
                store.db.execute("BEGIN TRANSACTION")
                try:
                    store.put_bar("AMD","1Min",injected_bar(datetime.combine(day,time(9,1),ET)))
                    store.put_bar("AMD","1Min",injected_bar(datetime.combine(day,time(9,40),ET)))
                    store.put_bar("AMD","1Day",injected_bar(datetime.combine(day,time(0),ET)))
                    stamp=(cut+timedelta(seconds=1)).astimezone(timezone.utc).isoformat()
                    store.put_news({"id":"v2-future-"+str(day),"created_at":stamp,"updated_at":stamp,
                                    "headline":"Future outcome","summary":"Future outcome","symbols":["AMD"]})
                    store.put_fundamentals([["AMD","audit","FutureReturn","ratio",day,day,"AUDIT",str(day),
                                             cut+timedelta(seconds=1),999.0,"future",cut,"future","hash"]])
                    for mode in ExamMode:
                        packet=get_v2_snapshot(day,cut,symbols,mode=mode,_store=store)
                        prompt=build_exam_prompt(day,cut,symbols,mode=mode,_store=store,
                                                 simulated_account=audit_account(cut))
                        if digest(packet)!=originals[(day,mode.value)] or prompt.input_sha256!=prompt_hashes[(day,mode.value)]:
                            mismatches.append((str(day),mode.value))
                finally:store.db.execute("ROLLBACK")
                if day_index%10==0:print(f"V2 isolated injection {day_index}/60",flush=True)
        # A separate copied database validates intraday cutoffs; no source mutation.
        for day in (days[0],days[30],days[-3]):
            for at in (time(9,0),time(9,35),time(9,35,30)):
                cutoff=datetime.combine(day,at,ET)
                for mode in ExamMode:
                    packet=get_v2_snapshot(day,cutoff,symbols,mode=mode,db_path=copied)
                    intraday_failures.extend((str(day),str(at),mode.value,*x) for x in check(packet,cutoff,mode))
    for day in (date(2026,8,27),date(2026,9,25),date(2026,11,5)):
        cut=datetime.combine(day,time(9),ET)
        for mode in ExamMode:
            prompt=build_exam_prompt(day,cut,symbols,mode=mode,db_path=V2,
                                     simulated_account=audit_account(cut))
            target=OUT/f"{day}_0900_{mode.value.lower()}_ai_input.json"
            target.write_text(json.dumps({"messages":prompt.messages,"model":prompt.model,
                                          "snapshot_sha256":prompt.snapshot_sha256,
                                          "input_sha256":prompt.input_sha256,"risk_flags":prompt.risk_flags},
                                         ensure_ascii=False,indent=2),encoding="utf-8")
    v1_after=sha(V1);v2_after=sha(V2)
    report={"version":"exam-2026-11-05-v2","days":len(days),"trade_symbols":len(symbols),
            "market_reference":"QQQ Nasdaq-100 ETF proxy","v1_sha256_before":v1_before,
            "v1_sha256_after":v1_after,"v2_sha256_before":v2_before,"v2_sha256_after":v2_after,
            "raw_counts":raw_counts,"corporate_actions":corporate,"identity_status":identity,
            "snapshot_counts":{k:dict(v) for k,v in counts.items()},
            "timestamp_identity_failures":errors[:20],"timestamp_identity_failure_count":len(errors),
            "future_injection_mismatches":mismatches,"intraday_failures":intraday_failures[:20],
            "intraday_failure_count":len(intraday_failures),"sample_inputs":6}
    if v1_before!=v1_after or v2_before!=v2_after:raise RuntimeError("Source database changed during audit")
    (OUT/"audit_results.json").write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str),encoding="utf-8")
    (OUT/"all_60_hashes.json").write_text(json.dumps(day_summary,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(report,ensure_ascii=False,default=str),flush=True)
    if errors or mismatches or intraday_failures or raw_counts["old_spcx"] or raw_counts["non_iex"]:
        raise SystemExit(1)


if __name__=="__main__":main()
