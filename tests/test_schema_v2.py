import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from exam_replay.legacy_decision_schema import (
    Candidate,
    Condition,
    MarketDecision,
    OpportunityGrade,
    OrderIntent,
    PremarketPlan,
    RecheckDecision,
    RecheckResponse,
    ScanCandidate,
    SchemaError,
    SetupType,
    StopLoss,
    StopPolicy,
    TradeDecision,
    Trigger,
    TriggerPurpose,
    UniverseScan,
    as_intent,
    parse_plan,
    parse_recheck,
    parse_scan,
)
from trading_bot.deepseek_client import AICallError, DeepSeek

NOW = datetime(2026, 11, 5, 14, 5, tzinfo=timezone.utc)


def order_intent(**overrides):
    value = {
        "action": "BUY", "symbol": "CRWV", "qty": 3, "order_type": "limit",
        "limit_price": "100.20", "time_in_force": "day", "extended_hours": False,
        "stop_policy": "NONE", "stop_loss": None,
    }
    value.update(overrides)
    return value


def candidate(**overrides):
    value = {
        "symbol": "CRWV", "setup_type": "PULLBACK_RECLAIM", "opportunity_grade": "A",
        "trade_decision": "BUY", "analysis": "Fixture only, not an investment signal.",
        "plan_id": "plan-1", "plan_valid_until_utc": (NOW + timedelta(minutes=40)).isoformat(),
        "triggers": [{
            "trigger_id": "exec-1", "purpose": "EXECUTE_ORDER",
            "all_of": [{"field": "last", "op": "<=", "value": 101.0}],
        }],
        "order_intent": order_intent(),
    }
    value.update(overrides)
    return value


def plan(**overrides):
    value = {
        "schema_version": "2.0", "mode": "PREMARKET_PLAN",
        "as_of_utc": NOW.isoformat(), "model_id": "fixture",
        "market_decision": "TRADE", "account_snapshot_id": "snap-1",
        "candidates": [candidate()],
    }
    value.update(overrides)
    return value


def test_numeric_evidence_ids_are_normalized_to_strings():
    candidate = ScanCandidate(
        symbol="CRWV", setup_type=SetupType.PULLBACK_RECLAIM,
        opportunity_grade=OpportunityGrade.A, trade_decision=TradeDecision.WAIT,
        thesis="fixture", evidence_ids=[1, 2, "3"], priority=1,
    )
    assert candidate.evidence_ids == ["1", "2", "3"]


def test_valid_plan_and_qty_is_preserved(bundle):
    parsed = parse_plan(json.dumps(plan()), symbols=bundle.symbols, now=NOW)
    assert parsed.candidates[0].order_intent.qty == 3
    intent = as_intent(parsed.candidates[0])
    assert intent["side"] == "buy"
    assert intent["qty"] == 3
    assert intent["stop_policy"] == "NONE"


@pytest.mark.parametrize("qty", [0, -1, 1.5, "3", True])
def test_qty_must_be_positive_integer(qty, bundle):
    with pytest.raises((ValueError, SchemaError)):
        parse_plan(json.dumps(plan(candidates=[candidate(order_intent=order_intent(qty=qty))])),
                   symbols=bundle.symbols, now=NOW)


@pytest.mark.parametrize("change", [
    {"order_type": "market"}, {"time_in_force": "ioc"}, {"extended_hours": True},
    {"notional": 100}, {"limit_price": "100.201"}, {"limit_price": 0},
])
def test_forbidden_order_fields(change, bundle):
    with pytest.raises((ValueError, SchemaError)):
        parse_plan(json.dumps(plan(candidates=[candidate(order_intent=order_intent(**change))])),
                   symbols=bundle.symbols, now=NOW)


def test_none_with_stop_is_rejected(bundle):
    with pytest.raises((ValueError, SchemaError)):
        parse_plan(json.dumps(plan(candidates=[candidate(
            order_intent=order_intent(stop_loss={"stop_price": 90})
        )])), symbols=bundle.symbols, now=NOW)


def test_broker_stop_requires_valid_stop(bundle):
    with pytest.raises((ValueError, SchemaError)):
        parse_plan(json.dumps(plan(candidates=[candidate(
            order_intent=order_intent(stop_policy="BROKER_STOP")
        )])), symbols=bundle.symbols, now=NOW)
    with pytest.raises((ValueError, SchemaError)):
        parse_plan(json.dumps(plan(candidates=[candidate(
            order_intent=order_intent(stop_policy="BROKER_STOP",
                                      stop_loss={"stop_price": 101})
        )])), symbols=bundle.symbols, now=NOW)
    parsed = parse_plan(json.dumps(plan(candidates=[candidate(
        order_intent=order_intent(stop_policy="BROKER_STOP",
                                  stop_loss={"stop_price": 95})
    )])), symbols=bundle.symbols, now=NOW)
    assert parsed.candidates[0].order_intent.stop_loss.stop_price == 95


def test_broker_stop_on_sell_is_rejected(bundle):
    with pytest.raises((ValueError, SchemaError)):
        Candidate(
            symbol="CRWV", setup_type=SetupType.PULLBACK_RECLAIM,
            opportunity_grade=OpportunityGrade.A, trade_decision=TradeDecision.SELL,
            analysis="fixture", plan_id="sell-1",
            plan_valid_until_utc=NOW + timedelta(minutes=30),
            order_intent=OrderIntent(
                action="SELL", symbol="CRWV", qty=1, limit_price=100,
                stop_policy=StopPolicy.BROKER_STOP, stop_loss=StopLoss(stop_price=110),
            ),
        )


def test_outside_watchlist_and_duplicates(bundle):
    with pytest.raises(SchemaError):
        parse_plan(json.dumps(plan(candidates=[candidate(symbol="OUTSIDE", plan_id="x")])),
                   symbols=bundle.symbols, now=NOW)
    with pytest.raises((ValueError, SchemaError)):
        parse_plan(json.dumps(plan(candidates=[candidate(), candidate(plan_id="plan-2")])),
                   symbols=bundle.symbols, now=NOW)
    with pytest.raises((ValueError, SchemaError)):
        parse_plan(json.dumps(plan(candidates=[candidate(plan_id="same"),
                                                candidate(symbol="NBIS", plan_id="same")])),
                   symbols=bundle.symbols, now=NOW)


def test_expired_plan_rejected(bundle):
    with pytest.raises(SchemaError):
        parse_plan(json.dumps(plan(candidates=[candidate(
            plan_valid_until_utc=(NOW - timedelta(minutes=1)).isoformat()
        )])), symbols=bundle.symbols, now=NOW)


def test_trigger_without_order_or_order_without_trigger(bundle):
    with pytest.raises((ValueError, SchemaError)):
        Candidate(
            symbol="CRWV", setup_type=SetupType.PULLBACK_RECLAIM,
            opportunity_grade=OpportunityGrade.A, trade_decision=TradeDecision.HOLD,
            analysis="fixture", plan_id="hold-1",
            plan_valid_until_utc=NOW + timedelta(minutes=30),
            triggers=[Trigger(trigger_id="t", purpose=TriggerPurpose.EXECUTE_ORDER,
                              all_of=[Condition(field="last", op="<=", value=1)])],
        )


def test_condition_aliases_and_extra_fields(bundle):
    parsed = parse_plan(json.dumps(plan(candidates=[candidate(triggers=[{
        "trigger_id": "t", "purpose": "EXECUTE_ORDER",
        "all_of": [{"field": "last_trade_price", "op": "<=", "value": 1}],
    }])])), symbols=bundle.symbols, now=NOW)
    assert parsed.candidates[0].triggers[0].all_of[0].field.value == "last"
    with pytest.raises((ValueError, SchemaError)):
        parse_plan(json.dumps({**plan(), "unknown": 1}), symbols=bundle.symbols, now=NOW)


def test_scan_and_recheck_schema(bundle):
    scan = UniverseScan(
        as_of_utc=NOW, model_id="fixture", account_snapshot_id="snap",
        candidates=[ScanCandidate(
            symbol="CRWV", setup_type=SetupType.PULLBACK_RECLAIM,
            opportunity_grade=OpportunityGrade.A, trade_decision=TradeDecision.BUY,
            thesis="fixture", priority=1,
        )],
    )
    assert parse_scan(scan.model_dump_json(), symbols=bundle.symbols, now=NOW).candidates[0].symbol == "CRWV"
    recheck = RecheckResponse(
        as_of_utc=NOW, plan_id="plan-1", decision=RecheckDecision.CONTINUE,
        reason="fixture",
    )
    assert parse_recheck(recheck.model_dump_json(), plan_id="plan-1", now=NOW).decision is RecheckDecision.CONTINUE
    with pytest.raises(SchemaError):
        parse_recheck(recheck.model_dump_json(), plan_id="other", now=NOW)


class FakeCompletions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("no response")
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        content, finish = value
        return SimpleNamespace(
            model="fixture", usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
            choices=[SimpleNamespace(finish_reason=finish, message=SimpleNamespace(content=content))],
        )


def _client(responses, **kwargs):
    const = FakeCompletions(responses)
    fake = SimpleNamespace(chat=SimpleNamespace(completions=const))
    return fake, const


def test_deepseek_records_usage_and_repairs_invalid_json(tmp_path):
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "one_shot_decision.txt").write_text("system", encoding="utf-8")
    fake, calls = _client([("not json", "stop"), ('{"ok": true}', "stop")])
    records, inputs = [], []
    ai = DeepSeek(
        "fake", client=fake, prompt_dir=prompts, repair_attempts=1,
        record=records.append, input_record=inputs.append,
    )
    parsed, meta = ai.complete("ONE_SHOT_DECISION", {"a": 1}, lambda raw: json.loads(raw))
    assert parsed == {"ok": True}
    assert meta["attempts"] == 2
    assert meta["prompt_tokens"] == 10
    assert len(calls.calls) == 2
    assert inputs and inputs[0]["mode"] == "ONE_SHOT_DECISION"
    assert [r["outcome"] for r in records] == ["schema_error", "ok"]


def test_deepseek_finish_reason_and_transport_errors(tmp_path):
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "one_shot_decision.txt").write_text("system", encoding="utf-8")
    fake, _ = _client([('{"ok": true}', "length")])
    ai = DeepSeek("fake", client=fake, prompt_dir=prompts, repair_attempts=0,
                  record=lambda record: None)
    with pytest.raises(AICallError):
        ai.complete("ONE_SHOT_DECISION", {}, lambda raw: json.loads(raw))
    fake, _ = _client([TimeoutError("offline")])
    ai = DeepSeek("fake", client=fake, prompt_dir=prompts, repair_attempts=0,
                  record=lambda record: None)
    with pytest.raises(AICallError):
        ai.complete("ONE_SHOT_DECISION", {}, lambda raw: json.loads(raw))
