"""Freeze the audited V2 DuckDB; source V1 is verified and never opened writable."""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime,timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from exam_data import ExamStore  # noqa:E402

V1=ROOT/"data/exam_data.duckdb"
V2=ROOT/"data/exam_v2.duckdb"
AUDIT=ROOT/"data/exam_v2_audit/audit_results.json"


def sha(path):
    with path.open("rb") as handle:return hashlib.file_digest(handle,"sha256").hexdigest()


def main():
    v1=json.loads((ROOT/"data/exam_data.manifest.json").read_text(encoding="utf-8"))
    audit=json.loads(AUDIT.read_text(encoding="utf-8"))
    if sha(V1)!=v1["db_sha256"]:raise RuntimeError("V1 hash changed")
    if audit["v2_sha256_after"]!=sha(V2):raise RuntimeError("V2 changed after audit")
    if audit["days"]!=60 or audit["timestamp_identity_failure_count"] or audit["future_injection_mismatches"] or audit["intraday_failure_count"]:
        raise RuntimeError("V2 audit has unresolved deterministic failures")
    if audit["raw_counts"]["old_spcx"] or audit["raw_counts"]["non_iex"]:
        raise RuntimeError("V2 still contains invalid issuer or feed")
    with ExamStore(V2) as store:
        if store.db.execute("SELECT value FROM meta WHERE key='freeze_version'").fetchone():
            raise RuntimeError("V2 already frozen")
        store.meta("freeze_version","exam-2026-11-05-v2")
        store.meta("freeze_at",datetime.now(timezone.utc).isoformat())
        store.db.execute("CHECKPOINT")
    manifest={"version":"exam-2026-11-05-v2","parent_v1_sha256":v1["db_sha256"],
              "db_sha256":sha(V2),"db_bytes":V2.stat().st_size,"feed":"iex",
              "adjustment":"raw","days":60,"trade_symbols":48,
              "market_reference":"QQQ Nasdaq-100 ETF proxy, not Nasdaq Composite",
              "counts":audit["raw_counts"],"strict":audit["snapshot_counts"]["STRICT"],
              "exploratory":audit["snapshot_counts"]["EXPLORATORY"],
              "audit_file":"data/exam_v2_audit/audit_results.json",
              "pre_freeze_sha256":audit["v2_sha256_after"],
              "content_version_status":"UNKNOWN for later-retrieved IEX bars, news bodies and SEC facts"}
    (ROOT/"data/exam_v2.manifest.json").write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(manifest,ensure_ascii=False),flush=True)


if __name__=="__main__":main()
