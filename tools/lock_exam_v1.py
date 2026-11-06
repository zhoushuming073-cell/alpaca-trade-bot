"""Record V1 evidence hashes and make the original files read-only on disk."""
from __future__ import annotations

import hashlib
import json
import os
import stat
from datetime import datetime,timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
DATA=ROOT/"data"


def sha(path):
    with path.open("rb") as handle:return hashlib.file_digest(handle,"sha256").hexdigest()


def main():
    manifest=json.loads((DATA/"exam_data.manifest.json").read_text(encoding="utf-8"))
    db=DATA/"exam_data.duckdb"
    if sha(db)!=manifest["db_sha256"]:raise RuntimeError("V1 original database changed")
    paths=[db,DATA/"exam_data.manifest.json",ROOT/"lookahead_audit_report.md"]
    paths+=sorted((DATA/"lookahead_audit").glob("*"))
    paths=[p for p in paths if p.is_file()]
    records={p.relative_to(ROOT).as_posix():{"sha256":sha(p),"bytes":p.stat().st_size} for p in paths}
    output=DATA/"v1_original_audit.manifest.json"
    output.write_text(json.dumps({"recorded_at_utc":datetime.now(timezone.utc).isoformat(),
                                  "files":records},ensure_ascii=False,indent=2),encoding="utf-8")
    for path in paths+[output]:
        path.chmod(stat.S_IREAD)
    if any(sha(ROOT/name)!=info["sha256"] for name,info in records.items()):
        raise RuntimeError("V1 evidence hash changed during protection")
    print(json.dumps({"protected_files":len(paths)+1,"v1_sha256":sha(db),
                      "manifest":str(output)},ensure_ascii=False),flush=True)


if __name__=="__main__":main()
