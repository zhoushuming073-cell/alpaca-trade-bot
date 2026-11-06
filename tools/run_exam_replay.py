"""CLI for a frozen, point-in-time exam replay.

Examples:
  .venv/Scripts/python tools/run_exam_replay.py --days 2026-08-13 2026-09-25 2026-11-05
  .venv/Scripts/python tools/run_exam_replay.py --last 10 --out data/exam_replay_runs/10day.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from exam_replay.runner import ExamClient, ExamReplay, write_report  # noqa: E402
from exam_v2.snapshot import ExamMode  # noqa: E402
from exam_data import ExamStore  # noqa: E402
from exam_data.store import ET  # noqa: E402
from datetime import date  # noqa: E402
import yaml  # noqa: E402


def exam_days(db_path, last=None):
    with ExamStore(db_path, read_only=True) as store:
        rows = store.db.execute("SELECT day FROM days ORDER BY day").fetchall()
    days = [str(row[0]) for row in rows]
    return days[-last:] if last else days


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", nargs="*", default=[])
    parser.add_argument("--last", type=int, default=0)
    parser.add_argument("--mode", choices=["strict", "exploratory"], default="exploratory")
    parser.add_argument("--model", default="deepseek-flash")
    parser.add_argument("--max-tokens", type=int, default=7000)
    parser.add_argument("--max-focus", type=int, default=4)
    parser.add_argument("--out", default="data/exam_replay_runs/latest.json")
    parser.add_argument("--dry-run", action="store_true",
                        help="build a local fake decision; no provider call")
    parser.add_argument("--three-agent", action="store_true",
                        help="run the in-process researcher/skeptic/PM flow instead of single-agent")
    args = parser.parse_args()
    db_path = ROOT / "data/exam_v2.duckdb"
    if not db_path.exists():
        raise SystemExit("Missing data/exam_v2.duckdb")
    days = args.days or exam_days(db_path, args.last or 10)
    if not days:
        raise SystemExit("No exam days selected")
    symbols = yaml.safe_load((ROOT / "config/watchlist.yaml").read_text(encoding="utf-8"))["symbols"]
    transport = None
    if args.dry_run:
        from tests.test_exam_replay import FakeTransport
        transport = FakeTransport()
    else:
        from trading_bot.dashboard import worker_environment
        env = worker_environment()
        transport = ExamClient(api_key=env["DEEPSEEK_API_KEY"], model=args.model,
                               max_tokens=args.max_tokens)
    replay = ExamReplay(
        symbols=symbols, db_path=db_path, mode=ExamMode(args.mode.upper()),
        model=args.model, transport=transport, max_focus=args.max_focus,
        max_tokens=args.max_tokens, short_locates=True,
    )
    result = replay.run_three_agent(days) if args.three_agent else replay.run(days)
    report = replay.report(result)
    report["engine_mode"] = "THREE_AGENT" if args.three_agent else "SINGLE_AGENT"
    report["days"] = days
    report["dry_run"] = bool(args.dry_run)
    report["budget"] = {
        "max_tokens_per_call": args.max_tokens,
        "total_token_limit": None,
        "note": "No global cap in replay CLI; inspect calls and pass a bounded day list.",
    }
    out = Path(args.out)
    if not out.is_absolute():
        out = ROOT / out
    write_report(report, out)
    metrics = report.get("metrics") or {}
    print(json.dumps({
        "out": str(out), "days": days, "mode": report["exam_mode"],
        "model": args.model, "status": "OK",
        "metrics": {k: metrics.get(k) for k in (
            "total_return", "max_drawdown", "win_rate", "trades",
            "qqq_buy_hold_return", "long", "short",
        )},
        "calls": len(report.get("calls", [])),
        "tokens": sum((c.get("prompt_tokens") or 0) + (c.get("completion_tokens") or 0)
                      for c in report.get("calls", [])),
        "no_real_orders": True,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
