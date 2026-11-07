"""Offline acceptance tests for the full intraday exam replay."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from exam_replay.full import FullExamReplay
from exam_replay.legacy_decision_schema import (
    MarketDecision, PositionReview, PremarketPlan, RecheckResponse, UniverseScan,
)

ET = ZoneInfo("America/New_York")


def test_exam_execution_uses_noon_window():
    from exam_replay.broker import ExamBroker
    from exam_v3.contracts import time_state

    broker = ExamBroker(["AAPL"])
    before = datetime(2026, 9, 28, 11, 59, 29, tzinfo=ET)
    cutoff = datetime(2026, 9, 28, 11, 59, 30, tzinfo=ET)
    noon = datetime(2026, 9, 28, 12, 0, tzinfo=ET)
    assert time_state(before)["new_order_allowed"] is True
    assert time_state(cutoff)["trading_window_open"] is True
    assert time_state(cutoff)["new_order_allowed"] is False
    assert time_state(noon)["trading_window_open"] is False
    broker.submit(symbol="AAPL", action="BUY", qty=1, limit_price=100,
                  submitted_at=before)
    with pytest.raises(ValueError, match="11:59:30"):
        broker.submit(symbol="AAPL", action="BUY", qty=1, limit_price=100,
                      submitted_at=cutoff)


class FakeAI:
    def __init__(self):
        self.requests = []

    def complete(self, mode, payload, parser, **kwargs):
        self.requests.append((mode, payload))
        as_of = payload.get("as_of_utc")
        account_id = payload.get("account_snapshot_id") or payload.get("account", {}).get("account_snapshot_id")
        if mode == "UNIVERSE_SCAN":
            data = UniverseScan(
                as_of_utc=as_of, model_id="deepseek-flash", account_snapshot_id=account_id,
                candidates=[{
                    "symbol": "AAPL", "setup_type": "PULLBACK_RECLAIM",
                    "opportunity_grade": "B", "trade_decision": "OPEN_LONG",
                    "thesis": "fixture", "evidence_ids": [], "priority": 1,
                }],
            )
        elif mode == "PREMARKET_PLAN":
            focus = payload["focus"][0]["symbol"]
            data = PremarketPlan(
                as_of_utc=as_of, model_id="deepseek-flash", account_snapshot_id=account_id,
                market_decision=MarketDecision.TRADE,
                candidates=[{
                    "symbol": focus, "setup_type": "PULLBACK_RECLAIM",
                    "opportunity_grade": "B", "trade_decision": "OPEN_LONG",
                    "analysis": "fixture plan", "evidence_ids": [],
                    "plan_id": "p1", "plan_valid_until_utc": (
                        datetime.fromisoformat(str(as_of).replace("Z", "+00:00")) + timedelta(minutes=90)
                    ).isoformat(),
                    "triggers": [
                        {"trigger_id": "exec", "purpose": "EXECUTE_ORDER",
                         "all_of": [{"field": "last", "op": "<=", "value": 10000}]},
                        {"trigger_id": "recheck", "purpose": "REQUEST_AI_RECHECK",
                         "all_of": [{"field": "last", "op": ">=", "value": 0}]},
                    ],
                    "order_intent": {
                        "action": "OPEN_LONG", "symbol": focus, "qty": 1,
                        "limit_price": "10000", "order_type": "limit",
                        "time_in_force": "day", "extended_hours": False,
                        "stop_policy": "BROKER_STOP",
                        "stop_loss": {"stop_price": "1"},
                    },
                }],
            )
        elif mode == "POSITION_REVIEW":
            data = PositionReview(as_of_utc=as_of, instructions=[])
        else:
            plan_id = payload.get("plan_id", "p1")
            data = RecheckResponse(
                as_of_utc=as_of, plan_id=plan_id, decision="CONTINUE",
                reason="fixture continue",
            )
        return parser(data.model_dump_json()), {
            "prompt_tokens": 10, "completion_tokens": 5, "latency_ms": 1,
            "model": "fake", "prompt_hash": "x", "input_hash": "y", "output_hash": "z",
        }


def test_full_intraday_calls_recheck_and_carries_state():
    fake = FakeAI()
    replay = FullExamReplay(
        symbols=["AAPL"], api_key="not-used", db_path="data/exam_v2.duckdb",
        ai_client=fake, max_focus=1,
    )
    result = replay.run(["2026-09-28", "2026-09-29", "2026-09-30"])
    modes = [mode for mode, _ in fake.requests]
    assert modes.count("UNIVERSE_SCAN") == 3
    assert modes.count("PREMARKET_PLAN") == 3
    assert modes.count("INTRADAY_RECHECK") >= 1
    assert "POSITION_REVIEW" in modes
    assert len(result.days) == 3
    assert replay.broker.positions.get("AAPL", 0) >= 1
    assert replay.broker.fills
    assert replay.broker.active_stop("AAPL") is not None
    report = replay.report(result)
    assert report["metrics"]["total_return"] is not None
    assert report["no_real_orders"] is True
    assert report["execution_contract"]["allow_short"] is True


def test_broker_stop_gap_and_adjust_remove():
    from exam_replay.broker import ExamBroker
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    broker = ExamBroker(["AAPL"])
    at = datetime(2026, 9, 28, 9, 30, tzinfo=et)
    broker.submit(symbol="AAPL", action="OPEN_LONG", qty=10, limit_price=100,
                  submitted_at=at, stop_policy="BROKER_STOP", stop_loss=90)

    def bar(t, o, h, l, c):
        return {"symbol": "AAPL", "timeframe": "1Min", "feed": "iex",
                "ts": t, "end_at": t + timedelta(minutes=1),
                "open": o, "high": h, "low": l, "close": c, "volume": 10000}

    broker.process_bar(bar(datetime(2026, 9, 28, 9, 31, tzinfo=et), 100, 101, 99, 100))
    broker.adjust_stop("AAPL", stop_price=95, at=datetime(2026, 9, 28, 9, 32, tzinfo=et),
                       reason="fixture adjust")
    assert broker.active_stop("AAPL").stop_price == Decimal("95")
    broker.remove_stop("AAPL", at=datetime(2026, 9, 28, 9, 33, tzinfo=et), reason="fixture remove")
    assert broker.active_stop("AAPL") is None
    broker.set_stop("AAPL", stop_price=90, at=datetime(2026, 9, 28, 9, 34, tzinfo=et),
                    reason="fixture reset")
    fills = broker.process_bar(bar(datetime(2026, 10, 5, 10, 1, tzinfo=et), 80, 81, 79, 80))
    assert fills and fills[0].reason == "STOP_TRIGGERED"
    assert float(fills[0].price) < 90
    assert broker.positions == {}


def test_no_stop_position_survives_overnight():
    from exam_replay.broker import ExamBroker
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    broker = ExamBroker(["AAPL"])
    at = datetime(2026, 9, 28, 9, 30, tzinfo=et)
    broker.submit(symbol="AAPL", action="OPEN_LONG", qty=2, limit_price=100,
                  submitted_at=at, stop_policy="NONE")

    def bar(t, o, h, l, c):
        return {"symbol": "AAPL", "timeframe": "1Min", "feed": "iex",
                "ts": t, "end_at": t + timedelta(minutes=1),
                "open": o, "high": h, "low": l, "close": c, "volume": 10000}

    broker.process_bar(bar(datetime(2026, 9, 28, 9, 31, tzinfo=et), 100, 101, 99, 100))
    assert broker.positions["AAPL"] == 2
    assert broker.active_stop("AAPL") is None
    broker.expire_day(datetime(2026, 9, 28).date())
    assert broker.positions["AAPL"] == 2


def test_recheck_payload_has_current_position_and_risk_and_no_future():
    fake = FakeAI()
    replay = FullExamReplay(
        symbols=["AAPL"], api_key="not-used", db_path="data/exam_v2.duckdb",
        ai_client=fake, max_focus=1,
    )
    result = replay.run(["2026-09-28", "2026-09-29"])
    rechecks = [p for m, p in fake.requests if m == "INTRADAY_RECHECK"]
    assert rechecks
    payload = rechecks[-1]
    assert payload["quote"]["last"] > 0
    assert "positions" in payload["account"]
    assert "buying_power" in payload["account"]
    assert payload["account"]["shorting_enabled"] is True
    # No supplied bar ends after the recheck timestamp.
    asof = datetime.fromisoformat(payload["as_of_utc"].replace("Z", "+00:00"))
    for key in ("daily_recent", "premarket_recent"):
        for bar in payload["history"][key]:
            assert datetime.fromisoformat(bar["end_at"].replace("Z", "+00:00")) <= asof
    assert payload["plan"]["symbol"] == "AAPL"
    assert payload["trigger"]["purpose"] == "REQUEST_AI_RECHECK"


def test_invalidate_plan_cancels_and_does_not_submit_order():
    class InvalidateAI(FakeAI):
        def complete(self, mode, payload, parser, **kwargs):
            if mode != "PREMARKET_PLAN":
                return super().complete(mode, payload, parser, **kwargs)
            as_of = payload["as_of_utc"]
            focus = payload["focus"][0]["symbol"]
            data = PremarketPlan(
                as_of_utc=as_of, model_id="deepseek-flash",
                account_snapshot_id=payload["account_snapshot_id"],
                market_decision=MarketDecision.TRADE,
                candidates=[{
                    "symbol": focus, "setup_type": "EVENT_MISPRICING",
                    "opportunity_grade": "B", "trade_decision": "OPEN_LONG",
                    "analysis": "fixture invalidate", "evidence_ids": [],
                    "plan_id": "inv1", "plan_valid_until_utc": (
                        datetime.fromisoformat(str(as_of).replace("Z", "+00:00")) + timedelta(minutes=90)
                    ).isoformat(),
                    "triggers": [
                        {"trigger_id": "inval", "purpose": "INVALIDATE_PLAN",
                         "all_of": [{"field": "last", "op": ">=", "value": 0}]},
                        {"trigger_id": "exec", "purpose": "EXECUTE_ORDER",
                         "all_of": [{"field": "last", "op": "<=", "value": 10000}]},
                    ],
                    "order_intent": {
                        "action": "OPEN_LONG", "symbol": focus, "qty": 1,
                        "limit_price": "10000", "order_type": "limit",
                        "time_in_force": "day", "extended_hours": False,
                        "stop_policy": "NONE", "stop_loss": None,
                    },
                }],
            )
            self.requests.append((mode, payload))
            return parser(data.model_dump_json()), {"prompt_tokens": 1, "completion_tokens": 1,
                                                   "latency_ms": 1, "model": "fake"}

    fake = InvalidateAI()
    replay = FullExamReplay(symbols=["AAPL"], api_key="not-used",
                            db_path="data/exam_v2.duckdb", ai_client=fake, max_focus=1)
    replay.run(["2026-09-28"])
    assert not replay.broker.orders
    assert not replay.broker.fills


def test_15_contiguous_exam_sessions():
    from datetime import date
    from exam_data import ExamStore
    with ExamStore("data/exam_v2.duckdb", read_only=True) as store:
        rows = store.db.execute(
            "SELECT day FROM days WHERE day BETWEEN DATE '2026-09-28' AND DATE '2026-10-16' "
            "ORDER BY day"
        ).fetchall()
    days = [str(r[0]) for r in rows]
    assert days == [
        "2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02",
        "2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09",
        "2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16",
    ]
    assert len(days) == 15


def test_normalizes_numeric_stop_and_nontrading_order_intent():
    from exam_replay.parsing import normalize_plan, normalize_stop_metadata
    import json
    raw = json.dumps({
        "schema_version": "2.0", "mode": "PREMARKET_PLAN",
        "as_of_utc": "2026-09-28T13:00:00+00:00", "model_id": "x",
        "market_decision": "WATCH", "account_snapshot_id": "a",
        "candidates": [{
            "symbol": "AAPL", "setup_type": "PULLBACK_RECLAIM",
            "opportunity_grade": "B", "trade_decision": "HOLD",
            "analysis": "fixture", "plan_id": "p",
            "plan_valid_until_utc": "2026-09-28T13:30:00+00:00",
            "triggers": [{"trigger_id": "e", "purpose": "EXECUTE_ORDER",
                          "all_of": [{"field": "last", "op": ">=", "value": 1}]}],
            "order_intent": {"action": "OPEN_LONG", "symbol": "AAPL", "qty": 1,
                             "limit_price": 100, "stop_policy": "BROKER_STOP",
                             "stop_loss": 90},
        }],
    })
    cleaned = normalize_plan(normalize_stop_metadata(raw)[0])[0]
    data = json.loads(cleaned)
    assert data["candidates"][0]["order_intent"] is None
    assert data["candidates"][0]["triggers"] == []


def test_execution_mapping_and_no_alpaca_path():
    import inspect
    from exam_replay import full
    source = inspect.getsource(full)
    assert "alpaca" not in source.lower()
    assert "OrderService" not in source
    # Every requested production trigger and stop action has an explicit branch.
    assert "TriggerPurpose.REQUEST_AI_RECHECK" in source
    assert "TriggerPurpose.INVALIDATE_PLAN" in source
    assert "PositionAction.ADJUST_STOP" in source
    assert "PositionAction.REMOVE_STOP" in source
    assert "self.broker.process_bar" in source
