"""PIT snapshots for IEX-only STRICT and labeled EXPLORATORY examinations."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from contextlib import nullcontext

from exam_data import ExamStore, get_exam_snapshot
from exam_data.store import ET

DEFAULT_DB=Path(__file__).resolve().parents[2]/"data/exam_v2.duckdb"


class ExamMode(StrEnum):
    STRICT="STRICT"
    EXPLORATORY="EXPLORATORY"


def _calendar_aggregate(daily,frequency):
    groups={}
    for bar in daily:
        local=bar["ts"].astimezone(ET).date()
        period=local-timedelta(days=local.weekday()) if frequency=="weekly" else local.replace(day=1)
        key=bar["symbol"],period
        if key not in groups:
            groups[key]={"symbol":bar["symbol"],"period":period,"open":bar["open"],"high":bar["high"],
                         "low":bar["low"],"close":bar["close"],"volume":bar["volume"],
                         "end_at":bar["end_at"],"available_at":bar["available_at"],
                         "feed":bar["feed"],"adjustment":bar["adjustment"]}
        else:
            item=groups[key]
            item["high"]=max(item["high"],bar["high"])
            item["low"]=min(item["low"],bar["low"])
            item["close"]=bar["close"]
            item["volume"]+=bar["volume"]
            item["end_at"]=bar["end_at"]
            item["available_at"]=bar["available_at"]
    return list(groups.values())


def get_v2_snapshot(date_value,asof_time,symbols,*,mode=ExamMode.STRICT,db_path=None,_store=None):
    """Return a separate-mode packet from V2; never query production SQLite."""
    mode=ExamMode(mode)
    path=Path(db_path or DEFAULT_DB)
    with nullcontext(_store) if _store is not None else ExamStore(path,read_only=True) as store:
        version=store.db.execute("SELECT value FROM meta WHERE key='version'").fetchone()
        feed=store.db.execute("SELECT value FROM meta WHERE key='feed'").fetchone()
        if not version or version[0]!="exam-2026-11-05-v2" or not feed or feed[0]!="iex":
            raise ValueError("Only the prepared IEX V2 database is accepted")
        identity={r[0]:{"entity_id":r[1],"valid_from":r[2],"status":r[3]}
                  for r in store.db.execute("SELECT symbol,entity_id,valid_from,identity_status FROM security_identity").fetchall()}
        base=get_exam_snapshot(date_value,asof_time,symbols,_store=store)
        cutoff=datetime.fromisoformat(base["asof"])
        embargo=timedelta(minutes=15) if mode is ExamMode.STRICT else timedelta(seconds=30)
        kept=[];dropped={"identity":0,"availability":0}
        for bar in base["bars"]:
            if bar["feed"]!="iex":raise ValueError("Non-IEX bar in V2 snapshot")
            security=identity.get(bar["symbol"])
            if not security or bar["ts"].astimezone(ET).date()<security["valid_from"]:
                dropped["identity"]+=1;continue
            if bar["timeframe"]=="1Min" and bar["end_at"]>cutoff-embargo:
                dropped["availability"]+=1;continue
            if bar["end_at"]>cutoff or bar["available_at"]>cutoff:
                raise ValueError("Future bar crossed snapshot boundary")
            kept.append(bar)
        daily=[b for b in kept if b["timeframe"]=="1Day"]
        present={b["symbol"] for b in daily}
        if mode is ExamMode.STRICT:
            # SEC acceptance metadata is timestamped; no later-retrieved filing
            # text, news body, or current CompanyFacts value enters STRICT.
            evidence=[{"id":e["id"],"symbols":e["symbols"],"kind":"sec_filing_metadata",
                       "source":e["source"],"url":e["url"],"title":e["title"],
                       "first_published":e["first_published"],"historical_status":"ACCEPTANCE_TIME_ONLY"}
                      for e in base["evidence"] if e["kind"]=="sec_filing"]
            facts=[]
        else:
            evidence=[]
            for e in base["evidence"]:
                item={k:e[k] for k in ("id","symbols","kind","source","url","title","body",
                                      "first_published","updated_at","content_hash")}
                item["historical_status"]="UNKNOWN_HISTORICAL_CONTENT_VERSION"
                evidence.append(item)
            facts=[{**f,"historical_status":"UNKNOWN_HISTORICAL_VALUE_VERSION"}
                   for f in base["fundamentals"]]
        if any(e["first_published"]>cutoff or e.get("updated_at",e["first_published"])>cutoff for e in evidence):
            raise ValueError("Future evidence crossed snapshot boundary")
        if any(f["available_at"]>cutoff for f in facts):raise ValueError("Future fact crossed snapshot boundary")
        return {"schema_version":"exam-v2-snapshot-1","dataset_version":version[0],"mode":mode.value,
                "date":str(date.fromisoformat(str(date_value))),"asof_utc":cutoff.astimezone(timezone.utc).isoformat(),
                "feed":"iex","adjustment":"raw","symbols":list(symbols),
                "market_reference":base["market_reference"],"security_identity":{s:identity[s] for s in list(symbols)+base["market_reference"]},
                "bars":kept,"weekly_bars":_calendar_aggregate(daily,"weekly"),
                "monthly_bars":_calendar_aggregate(daily,"monthly"),"evidence":evidence,"fundamentals":facts,
                "unavailable_symbols":[s for s in symbols if s not in present],
                "dropped_rows":dropped,
                "limitations":{
                    "market_content":"IEX historical aggregates fetched later; revisions not independently archived",
                    "publication_latency":"STRICT uses a conservative 15-minute intraday embargo; exact original arrival unarchived",
                    "selection":"Current 48-symbol pool applied retrospectively; ex-post selection bias",
                    "model_pretraining":"A later AI model may know future events; cannot be proven absent",
                    "unknown_material":mode is ExamMode.EXPLORATORY}}
