"""Offline end-to-end tests for the frozen-exam replay runner."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from decimal import Decimal

from exam_v2.snapshot import ExamMode
from exam_replay.legacy_decision_schema import MarketDecision, PremarketPlan, UniverseScan

from exam_replay.runner import ExamReplay


class FakeTransport:
    def __init__(self):
        self.calls = []

    def __call__(self, system, user):
        payload = json.loads(user)
        self.calls.append(payload)
        as_of = payload["as_of_utc"]
        account_id = payload["account_snapshot_id"]
        if "UNIVERSE_SCAN" in system:
            scan = UniverseScan(
                as_of_utc=as_of, model_id="deepseek-flash", account_snapshot_id=account_id,
                candidates=[{
                    "symbol": "AAPL", "setup_type": "PULLBACK_RECLAIM",
                    "opportunity_grade": "B", "trade_decision": "OPEN_LONG",
                    "thesis": "fixture scan", "evidence_ids": [], "priority": 1,
                }],
            )
            return {"raw": scan.model_dump_json(), "prompt_tokens": 100,
                    "completion_tokens": 50, "latency_ms": 1}
        focus = payload["focus"][0]["symbol"]
        plan = PremarketPlan(
            as_of_utc=as_of, model_id="deepseek-flash", account_snapshot_id=account_id,
            market_decision=MarketDecision.TRADE,
            candidates=[{
                "symbol": focus, "setup_type": "PULLBACK_RECLAIM",
                "opportunity_grade": "B", "trade_decision": "OPEN_LONG",
                "analysis": "fixture plan", "evidence_ids": [],
                "plan_id": "exam-plan", "plan_valid_until_utc": (
                    datetime.fromisoformat(as_of.replace("Z", "+00:00")) + timedelta(minutes=30)
                ).isoformat(),
                "triggers": [{"trigger_id": "exec", "purpose": "EXECUTE_ORDER",
                              "all_of": [{"field": "last", "op": "<=", "value": 10000}]}],
                "order_intent": {
                    "action": "OPEN_LONG", "symbol": focus, "qty": 1,
                    "limit_price": "1000", "order_type": "limit", "time_in_force": "day",
                    "extended_hours": False, "stop_policy": "NONE", "stop_loss": None,
                },
            }],
        )
        return {"raw": plan.model_dump_json(), "prompt_tokens": 100,
                "completion_tokens": 50, "latency_ms": 1}


def test_fake_three_day_exam_replay_is_isolated_and_has_metrics():
    transport = FakeTransport()
    replay = ExamReplay(
        symbols=["AAPL", "AMD"], mode=ExamMode.EXPLORATORY,
        transport=transport, max_focus=2, ledger_path=None,
    )
    result = replay.run(["2026-09-25", "2026-09-28", "2026-09-29"])
    assert len(result.days) == 3
    assert result.equity_curve
    assert result.simulator is not None
    assert result.simulator.fills
    assert len(transport.calls) == 6
    assert all(payload["exam"]["no_external_research"] for payload in transport.calls)
    assert all(not payload["exam"]["current_memory_used"] for payload in transport.calls)
    report = replay.report(result)
    assert report["metrics"]["total_return"] is not None
    assert report["no_real_orders"] is True
    assert report["no_production_memory"] is True
    assert result.simulator.positions.get("AAPL", 0) > 0
