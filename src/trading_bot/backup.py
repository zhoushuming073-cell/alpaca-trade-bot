"""Consistent, verified SQLite backups outside Git; never deletes old snapshots."""
import argparse
import hashlib
import json
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import portalocker

from .settings import ROOT, paper_db_path, backup_directory


def backup_database(source, destination):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if destination == ROOT or ROOT in destination.parents:
        raise ValueError("Backup directory must be outside the Git checkout")
    if not source.is_file():
        raise FileNotFoundError("Live database missing; refusing to create an empty backup")
    destination.mkdir(parents=True, exist_ok=True)
    with portalocker.Lock(str(destination / "backup.lock"), timeout=1):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        target = destination / f"paper-{stamp}-{uuid.uuid4().hex[:8]}.sqlite"
        temporary = target.with_suffix(".partial")
        origin = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=5)
        copy = sqlite3.connect(temporary)
        started = time.monotonic()
        def progress(status, remaining, total):
            if time.monotonic() - started > 60:
                raise TimeoutError("Backup timed out; previous snapshots remain intact")
        try:
            origin.backup(copy, pages=256, progress=progress, sleep=0.05)
            if copy.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Backup integrity check failed")
            counts = {}
            for table in ("events", "orders", "fills", "research_profiles", "ai_calls", "assistant_messages"):
                if copy.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                    counts[table] = copy.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            if not {"events", "orders", "research_profiles"} <= counts.keys():
                raise ValueError("Required trading tables missing")
            manifest_path = destination / "latest.json"
            previous = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else None
            if previous and previous.get("source") != str(source):
                raise ValueError("Backup directory belongs to a different database")
            if previous and any(counts.get(k, 0) < v for k, v in previous["counts"].items()):
                raise ValueError("Database record counts regressed; preserve prior backup and investigate")
        finally:
            origin.close()
            copy.close()
        temporary.replace(target)
        with target.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        report = {"source": str(source), "path": str(target), "created_at": datetime.now(timezone.utc).isoformat(),
                  "counts": counts, "integrity": "ok", "sha256": digest}
        pending = destination / "latest.pending.json"
        pending.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        pending.replace(destination / "latest.json")
        return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--destination", default=str(backup_directory()))
    args = parser.parse_args()
    print(json.dumps(backup_database(paper_db_path(), args.destination), ensure_ascii=False))


if __name__ == "__main__":
    main()
