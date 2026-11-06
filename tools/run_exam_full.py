"""Freeze and run the single-agent 15-session historical exam."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from exam_data import ExamStore  # noqa: E402
from exam_replay.full import FullExamReplay  # noqa: E402
from exam_replay.runner import write_report  # noqa: E402
from trading_bot.dashboard import worker_environment  # noqa: E402
import yaml  # noqa: E402

DATES = [
    "2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02",
    "2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09",
    "2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16",
]
FILES = [
    "prompts/universe_scan.txt", "prompts/premarket_plan.txt",
    "prompts/intraday_recheck.txt", "prompts/position_review.txt",
    "config/strategy.yaml", "config/execution.yaml", "config/watchlist.yaml",
    "src/exam_replay/full.py", "src/exam_replay/broker.py",
    "src/exam_replay/runner.py", "src/exam_replay/parsing.py",
    "data/exam_v2.manifest.json",
]


def sha(path):
    return hashlib.file_digest((ROOT / path).open("rb"), "sha256").hexdigest()


def freeze():
    payload = {
        "model": "deepseek-flash",
        "exam_mode": "EXPLORATORY",
        "initial_cash": 100000,
        "initial_positions": "FLAT",
        "dates": DATES,
        "files": {path: sha(path) for path in FILES},
        "execution_overrides": {
            "allow_shorting": True,
            "allow_broker_stop": True,
            "broker_stop_verified": True,
            "allow_adjust_stop": True,
            "allow_remove_stop": True,
            "short_locate_assumption": "ALL_SYMBOLS_ASSUMED_AVAILABLE_FOR_EXAM",
            "entry_tif": "DAY",
            "protective_stop_tif": "GTC",
        },
        "strategy_changed": False,
        "prompts_changed": False,
    }
    payload["combined_sha256"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    payload["created_at"] = __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc).isoformat()
    return payload


def verify(payload):
    current = freeze()
    current.pop("created_at")
    expected = dict(payload)
    expected.pop("created_at")
    if current != expected:
        raise SystemExit("FROZEN CONFIG/PROMPT/CODE HASH MISMATCH; refusing to run")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--freeze-only", action="store_true")
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--out", default="data/exam_replay_runs/full15")
    args = parser.parse_args()
    out = ROOT / args.out
    out.mkdir(parents=True, exist_ok=True)
    freeze_path = out / "freeze.json"
    if not freeze_path.exists():
        freeze_path.write_text(json.dumps(freeze(), ensure_ascii=False, indent=2), encoding="utf-8")
        print(freeze_path)
    if args.freeze_only:
        return
    frozen = json.loads(freeze_path.read_text(encoding="utf-8"))
    verify(frozen)
    actual = DATES[:1] if args.smoke else DATES
    with ExamStore(ROOT / "data/exam_v2.duckdb", read_only=True) as store:
        rows = store.db.execute(
            "SELECT day FROM days WHERE day>=DATE '2026-09-28' AND day<=DATE '2026-10-16' ORDER BY day"
        ).fetchall()
    available = [str(r[0]) for r in rows]
    if available != DATES:
        raise SystemExit("15-session calendar mismatch: " + repr(available))
    symbols = yaml.safe_load((ROOT / "config/watchlist.yaml").read_text(encoding="utf-8"))["symbols"]
    env = worker_environment()
    audit_path = out / ("smoke_audit.jsonl" if args.smoke else "audit.jsonl")
    if audit_path.exists():
        audit_path.unlink()
    audit = audit_path.open("w", encoding="utf-8")

    def hook(record):
        audit.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        audit.flush()

    replay = FullExamReplay(
        symbols=symbols, api_key=env["DEEPSEEK_API_KEY"],
        db_path=ROOT / "data/exam_v2.duckdb", model="deepseek-flash",
        max_tokens=7000, max_focus=4, initial_cash=100000,
        output_dir=out, audit_hook=hook,
    )
    try:
        result = replay.run(actual)
        report = replay.report(result)
        report["freeze"] = frozen
        report["smoke"] = bool(args.smoke)
        report["days"] = actual
        report["no_real_orders"] = True
        report["no_production_memory"] = True
        name = "smoke" if args.smoke else "report"
        write_report(report, out / f"{name}.json")
        print(json.dumps({
            "out": str(out / f"{name}.json"), "days": len(actual),
            "equity": result.equity_curve[-1], "return": report["metrics"].get("total_return"),
            "trades": len(result.trades), "fills": len(result.fills),
            "calls": len(report["calls"]), "tokens": sum(
                (c.get("prompt_tokens") or 0) + (c.get("completion_tokens") or 0)
                for c in report["calls"]),
        }, ensure_ascii=False, indent=2))
    finally:
        audit.close()


if __name__ == "__main__":
    main()
