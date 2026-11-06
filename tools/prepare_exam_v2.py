"""Create the V2 lineage and identity tables in a new, unfrozen exam database."""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from exam_data import ExamStore  # noqa:E402

V1=ROOT/"data/exam_data.duckdb"
V2=ROOT/"data/exam_v2.duckdb"
CORPORATE=ROOT/"data/lookahead_audit/corporate_actions.json"
SPACEX_IR="https://ir.spacex.com/updates/releases-details/2026/Space-Exploration-Technologies-Corp--Announces-Pricing-of-Initial-Public-Offering/default.aspx"


def sha(path):
    with path.open("rb") as handle:return hashlib.file_digest(handle,"sha256").hexdigest()


def main():
    original=json.loads((ROOT/"data/exam_data.manifest.json").read_text(encoding="utf-8"))
    if sha(V1)!=original["db_sha256"]:raise RuntimeError("V1 hash does not match its frozen manifest")
    if not V2.exists():raise FileNotFoundError("Collect IEX bars into a separate V2 database first")
    symbols=yaml.safe_load((ROOT/"config/watchlist.yaml").read_text(encoding="utf-8"))["symbols"]
    actions=json.loads(CORPORATE.read_text(encoding="utf-8"))
    with ExamStore(V2) as store:
        if store.db.execute("SELECT value FROM meta WHERE key='v2_prepared'").fetchone():
            raise RuntimeError("V2 already prepared")
        feed=store.db.execute("SELECT value FROM meta WHERE key='feed'").fetchone()[0]
        if feed!="iex":raise RuntimeError("V2 must use production-accessible IEX data")
        done=store.scalar("SELECT COUNT(*) FROM jobs WHERE kind='minute' AND job_key LIKE 'minute:iex:%' AND status='done'")
        if done!=60:raise RuntimeError(f"IEX minute collection incomplete: {done}/60")
        store.db.execute("ATTACH ? AS original (READ_ONLY)",[str(V1)])
        try:
            store.db.execute("INSERT OR REPLACE INTO evidence SELECT * FROM original.evidence")
            store.db.execute("INSERT OR REPLACE INTO fundamentals SELECT * FROM original.fundamentals")
        finally:store.db.execute("DETACH original")
        store.db.execute("""CREATE TABLE security_identity(symbol VARCHAR PRIMARY KEY,entity_id VARCHAR,
            entity_label VARCHAR,valid_from DATE,valid_to DATE,source VARCHAR,identity_status VARCHAR);
            CREATE TABLE corporate_action_audit(id VARCHAR PRIMARY KEY,kind VARCHAR,process_date DATE,
            retrieved_at TIMESTAMPTZ,source VARCHAR,historical_status VARCHAR,payload_json VARCHAR);
            CREATE TABLE v2_changes(change_id VARCHAR PRIMARY KEY,affected_rows BIGINT,reason VARCHAR,source VARCHAR,
            recorded_at TIMESTAMPTZ);""")
        old_spcx=store.scalar("SELECT COUNT(*) FROM bars WHERE symbol='SPCX' AND day<DATE '2026-06-12'")
        if old_spcx!=82:raise RuntimeError(f"Unexpected old SPCX IEX daily count: {old_spcx}")
        store.db.execute("DELETE FROM bars WHERE symbol='SPCX' AND day<DATE '2026-06-12'")
        store.db.execute("INSERT INTO v2_changes VALUES (?,?,?,?,?)",
            ["remove_old_spcx_issuer",old_spcx,"Ticker belonged to prior ETF or stale post-renaming bars before SpaceX IPO",
             SPACEX_IR,datetime.now(timezone.utc)])
        for symbol in symbols+["QQQ"]:
            if symbol=="SPCX":
                entity_id,label,start,source,status=("SPACEX_2026","Space Exploration Technologies Corporation",
                    date(2026,6,12),SPACEX_IR,"VERIFIED_IPO_DATE")
            elif symbol=="SKHY":
                entity_id,label,start,source,status=("SKHY_2026","SKHY (old ticker SKHYV)",
                    date(2026,7,13),"Alpaca corporate action id ccd157eb-d7f6-42cb-823a-2bf2215bc64e",
                    "CODE_CHANGE_CURRENT_API")
            else:
                entity_id,label,start,source,status=("CURRENT:"+symbol,symbol,date(2026,2,13),
                    "IEX observed daily bar, entity identity not independently time-archived","OBSERVED_NOT_VERIFIED")
            store.db.execute("INSERT INTO security_identity VALUES (?,?,?,?,?,?,?)",
                             [symbol,entity_id,label,start,None,source,status])
        for item in actions["records"]:
            data=item["data"]
            if data.get("id"):
                store.db.execute("INSERT OR REPLACE INTO corporate_action_audit VALUES (?,?,?,?,?,?,?)",
                    [data["id"],item["type"],data.get("process_date"),actions["retrieved_at"],actions["source"],
                     "CURRENT_API_CREATION_TIME_UNKNOWN",json.dumps(data,sort_keys=True)])
        store.meta("version","exam-2026-11-05-v2")
        store.meta("parent_v1_sha256",original["db_sha256"])
        store.meta("v2_data_feed","iex")
        store.meta("strict_intraday_embargo_seconds","900")
        store.meta("exploratory_intraday_embargo_seconds","30")
        store.meta("v2_prepared",datetime.now(timezone.utc).isoformat())
        store.db.execute("CHECKPOINT")
        print(json.dumps({"version":"exam-2026-11-05-v2","feed":"iex","minute_days":done,
            "removed_wrong_spcx_rows":old_spcx,"news":store.scalar("SELECT COUNT(*) FROM evidence WHERE kind='news'"),
            "filings":store.scalar("SELECT COUNT(*) FROM evidence WHERE kind='sec_filing'"),
            "facts":store.scalar("SELECT COUNT(*) FROM fundamentals"),
            "corporate_actions":store.scalar("SELECT COUNT(*) FROM corporate_action_audit"),
            "parent_v1_hash":original["db_sha256"]}),flush=True)


if __name__=="__main__":main()
