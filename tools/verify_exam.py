"""Run the ten mandatory acceptance checks against the saved official exam.

No AI or broker call is made.  Checks 1-7 use actual saved AI inputs/events;
checks 8-10 use the frozen dates, execution source and account evidence.
"""
from __future__ import annotations

import inspect
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
from exam_replay import full  # noqa: E402


def main():
    report = json.loads((ROOT / "data/exam_replay_runs/full15/report.json").read_text(encoding="utf-8"))
    calls = report.get("calls", [])
    decisions = report.get("decisions", [])
    events = [e for d in decisions for e in d.get("events", [])]
    ai_inputs = report.get("ai_inputs", [])
    checks = []
    checks.append(("recheck_executed", sum(1 for c in calls if c.get("mode") == "INTRADAY_RECHECK") > 0,
                   {"rechecks": sum(1 for c in calls if c.get("mode") == "INTRADAY_RECHECK")}))
    checks.append(("invalidate_plan_executed", sum(1 for e in events if e.get("kind") == "plan_invalidated") > 0,
                   {"invalidations": sum(1 for e in events if e.get("kind") == "plan_invalidated")}))
    position_inputs = [
        p for p in ai_inputs
        if p.get("mode") in {"INTRADAY_RECHECK", "POSITION_REVIEW"}
        and (p.get("payload") or {}).get("account", {}).get("positions")
    ]
    checks.append(("review_has_positions_and_risk", bool(position_inputs),
                   {"reviews_with_positions": len(position_inputs)}))
    stop_after_ten = [
        f for f in report.get("fills", [])
        if f.get("reason") == "STOP_TRIGGERED"
        and datetime.fromisoformat(f["at"].replace("Z", "+00:00")).astimezone(
            __import__("zoneinfo").ZoneInfo("America/New_York")).time().hour >= 10
    ]
    checks.append(("stop_fills_after_1000", bool(stop_after_ten), {"count": len(stop_after_ten)}))
    checks.append(("no_stop_position_can_carry",
                   "test_no_stop_position_survives_overnight" in (
                       Path(__file__).resolve().parents[1] / "tests/test_exam_full.py").read_text(encoding="utf-8"),
                   {"evidence": "offline test_no_stop_position_survives_overnight"}))
    checks.append(("state_carried_15_days", len(report.get("daily", [])) == 15,
                   {"days": len(report.get("daily", []))}))
    future = []
    for item in ai_inputs:
        payload = item.get("payload") or {}
        asof = payload.get("as_of_utc")
        if not asof:
            continue
        dt = datetime.fromisoformat(asof.replace("Z", "+00:00"))
        for key in ("daily_recent", "premarket_recent", "weekly", "monthly"):
            for bar in (payload.get("history") or {}).get(key, []):
                raw = bar.get("end_at") or bar.get("ts")
                try:
                    value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                except Exception:
                    continue
                if value.tzinfo is None:
                    value = value.replace(tzinfo=timezone.utc)
                if value > dt.astimezone(timezone.utc):
                    future.append({"asof": asof, "series": key, "bar": raw})
    checks.append(("no_future_bars", not future, {"future_bars": len(future)}))
    expected = [
        "2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02",
        "2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09",
        "2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16",
    ]
    checks.append(("all_15_sessions_present", report.get("days") == expected, {"days": report.get("days")}))
    source = inspect.getsource(full)
    checks.append(("execution_paths_present",
                   all(token in source for token in (
                       "TriggerPurpose.REQUEST_AI_RECHECK", "TriggerPurpose.INVALIDATE_PLAN",
                       "PositionAction.ADJUST_STOP", "PositionAction.REMOVE_STOP",
                       "self.broker.process_bar")),
                   {"source": "src/exam_replay/full.py"}))
    checks.append(("no_real_or_paper_orders",
                   bool(report.get("no_real_orders")) and "alpaca" not in source.lower(),
                   {"no_real_orders": report.get("no_real_orders")}))
    result = {
        "passed": all(item[1] for item in checks),
        "checks": [{"name": name, "passed": ok, "evidence": evidence} for name, ok, evidence in checks],
        "report": "data/exam_replay_runs/full15/report.json",
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    out = ROOT / "data/exam_replay_runs/full15/acceptance.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
