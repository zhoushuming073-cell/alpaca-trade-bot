from datetime import datetime, timedelta, timezone
from dataclasses import replace

import pytest

from exam_replay.legacy_decision_schema import (
    Candidate,
    MarketDecision,
    OpportunityGrade,
    OrderIntent,
    PremarketPlan,
    SetupType,
    StopPolicy,
    TradeDecision,
    Trigger,
    TriggerPurpose,
    as_order_intent,
)
from trading_bot.dry_run import FakeBroker
from trading_bot.execution import OrderService
from trading_bot.risk_manager import RiskRejected, coverable_qty, sellable_qty, validate_new_intent

QUOTE = {"bid": 99.99, "ask": 100.01, "last": 100.0,
         "quote_age_seconds": 0.0, "last_age_seconds": 0.0}
NOW = datetime(2026, 11, 5, 14, 35, 10, tzinfo=timezone.utc)


def short_bundle(bundle, enabled=True):
    execution = bundle.execution.model_copy(update={"allow_shorting": enabled})
    return replace(
        bundle, execution=execution,
        hashes={**bundle.hashes, "combined": "short-test-" + str(enabled)},
    )


def account(*, shorting=True, buying_power=100000.0, quantities=None, **overrides):
    value = FakeBroker(shorting_enabled=shorting).snapshot()
    value["buying_power"] = buying_power
    value["cash"] = 100000.0
    value["last_equity"] = 100000.0
    value["equity"] = 100000.0
    value["quantities"] = dict(quantities or {})
    value["positions"] = {s: q * 100.0 for s, q in value["quantities"].items()}
    value["available_quantities"] = {s: abs(q) for s, q in value["quantities"].items()}
    value.update(overrides)
    return value


def intent(action, *, qty=10, price=100.0, **overrides):
    side = "buy" if action in {"OPEN_LONG", "COVER_SHORT", "BUY"} else "sell"
    position_intent = {
        "OPEN_LONG": "buy_to_open", "BUY": "buy_to_open",
        "CLOSE_LONG": "sell_to_close", "SELL": "sell_to_close",
        "OPEN_SHORT": "sell_to_open", "COVER_SHORT": "buy_to_close",
    }[action]
    value = {
        "action": action, "side": side, "position_intent": position_intent,
        "symbol": "CRWV", "qty": qty, "limit_price": price,
        "order_type": "limit", "time_in_force": "day", "extended_hours": False,
        "stop_policy": "NONE", "stop_loss": None,
    }
    value.update(overrides)
    return value


def validate(bundle, session, value, account_value=None):
    return validate_new_intent(
        value, account=account_value or account(shorting=True), quote=QUOTE,
        risk=bundle.risk, execution=bundle.execution, symbols=bundle.symbols,
        session=session, buy_attempts=0, local_orders=[], freeze_reason="",
        stop_allowed=False,
    )


def schema_order(value):
    return {key: item for key, item in value.items() if key not in {"side", "position_intent"}}


def test_shorting_is_off_by_default(bundle, session):
    assert bundle.execution.allow_shorting is False
    with pytest.raises(RiskRejected) as failure:
        validate(short_bundle(bundle, enabled=False), session, intent("OPEN_SHORT"))
    assert failure.value.code == "SHORTING_DISABLED"


def test_short_open_requires_account_shorting_enabled(bundle, session):
    with pytest.raises(RiskRejected) as failure:
        validate(short_bundle(bundle), session, intent("OPEN_SHORT"),
                 account(shorting=False))
    assert failure.value.code == "SHORTING_NOT_ENABLED"


def test_short_open_uses_buying_power_and_preserves_qty(bundle, session):
    value = intent("OPEN_SHORT", qty=7, price=100.0)
    assert validate(short_bundle(bundle), session, value, account(buying_power=100000)) == 7
    with pytest.raises(RiskRejected) as failure:
        validate(short_bundle(bundle), session, value, account(buying_power=600.0))
    assert failure.value.code == "INSUFFICIENT_BUYING_POWER"


def test_short_and_long_cannot_cross_zero(bundle, session):
    with pytest.raises(RiskRejected) as failure:
        validate(short_bundle(bundle), session, intent("OPEN_LONG", qty=1),
                 account(quantities={"CRWV": -5}, shorting=True))
    assert failure.value.code == "WOULD_CROSS_ZERO"
    with pytest.raises(RiskRejected) as failure:
        validate(short_bundle(bundle), session, intent("OPEN_SHORT", qty=1),
                 account(quantities={"CRWV": 5}, shorting=True))
    assert failure.value.code == "WOULD_CROSS_ZERO"


def test_close_long_and_cover_short_are_distinct(bundle, session):
    long_account = account(quantities={"CRWV": 8})
    assert validate(short_bundle(bundle), session, intent("CLOSE_LONG", qty=8), long_account) == 8
    with pytest.raises(RiskRejected) as failure:
        validate(short_bundle(bundle), session, intent("COVER_SHORT", qty=1), long_account)
    assert failure.value.code == "NO_SHORT_POSITION"

    short_account = account(quantities={"CRWV": -8}, shorting=True)
    assert validate(short_bundle(bundle), session, intent("COVER_SHORT", qty=8), short_account) == 8
    with pytest.raises(RiskRejected) as failure:
        validate(short_bundle(bundle), session, intent("CLOSE_LONG", qty=1), short_account)
    assert failure.value.code == "NO_LONG_POSITION"
    assert sellable_qty(short_account, "CRWV") == 0
    assert coverable_qty(short_account, "CRWV") == 8


def test_cover_cannot_exceed_short_or_turn_into_long(bundle, session):
    short_account = account(quantities={"CRWV": -3}, shorting=True)
    with pytest.raises(RiskRejected) as failure:
        validate(short_bundle(bundle), session, intent("COVER_SHORT", qty=4), short_account)
    assert failure.value.code == "INSUFFICIENT_SHORT_SHARES"
    with pytest.raises(RiskRejected) as failure:
        validate(short_bundle(bundle), session, intent("OPEN_LONG", qty=1), short_account)
    assert failure.value.code == "WOULD_CROSS_ZERO"


def test_short_action_position_intent_and_idempotency_are_explicit(bundle):
    short = OrderIntent.model_validate(schema_order(intent("OPEN_SHORT", qty=3, price=100.0)))
    cover = OrderIntent.model_validate(schema_order(intent("COVER_SHORT", qty=3, price=100.0)))
    long_open = OrderIntent.model_validate(schema_order(intent("OPEN_LONG", qty=3, price=100.0)))
    assert (short.side, short.position_intent) == ("sell", "sell_to_open")
    assert (cover.side, cover.position_intent) == ("buy", "buy_to_close")
    assert (long_open.side, long_open.position_intent) == ("buy", "buy_to_open")
    assert OrderService.client_order_id("p", "t", as_order_intent(short)) != \
        OrderService.client_order_id("p", "t", as_order_intent(cover))


def test_short_stop_direction_is_enforced_by_schema(bundle):
    with pytest.raises(Exception):
        OrderIntent.model_validate(schema_order(intent(
            "OPEN_SHORT", stop_policy="BROKER_STOP", stop_loss={"stop_price": 95.0},
        )))
    value = OrderIntent.model_validate(schema_order(intent(
        "OPEN_SHORT", stop_policy="BROKER_STOP", stop_loss={"stop_price": 105.0},
    )))
    assert value.stop_policy is StopPolicy.BROKER_STOP


def test_short_candidate_protocol_parses():
    now = datetime.now(timezone.utc)
    candidate = Candidate(
        symbol="CRWV", setup_type=SetupType.EVENT_MISPRICING,
        opportunity_grade=OpportunityGrade.B,
        trade_decision=TradeDecision.OPEN_SHORT,
        analysis="Price structure and fundamentals support a short; invalidation is above resistance.",
        plan_id="short-1", plan_valid_until_utc=now + timedelta(minutes=20),
        triggers=[Trigger(trigger_id="short-exec", purpose=TriggerPurpose.EXECUTE_ORDER,
                          all_of=[{"field": "last", "op": "<=", "value": 100.0}])],
        order_intent=OrderIntent.model_validate(schema_order(intent("OPEN_SHORT", qty=2))),
    )
    assert candidate.order_intent.position_intent == "sell_to_open"
    plan = PremarketPlan(
        as_of_utc=now, model_id="fixture", market_decision=MarketDecision.TRADE,
        account_snapshot_id="snap", plan_id="plan", candidates=[candidate],
    )
    assert plan.candidates[0].trade_decision is TradeDecision.OPEN_SHORT


def test_short_account_context_keeps_signed_pnl_and_direction(bundle, tmp_path):
    from trading_bot.one_shot_engine import OneShotEngine
    from trading_bot.storage import Store

    store = Store(tmp_path / "short-context.sqlite", instance_id="short-context")
    broker = FakeBroker(shorting_enabled=True)
    broker.positions["CRWV"] = -5
    original_snapshot = broker.snapshot

    def snapshot(include_history=False):
        value = original_snapshot(include_history=include_history)
        value["position_details"]["CRWV"].update(
            avg_entry_price=105.0, unrealized_pl=-12.5,
            unrealized_plpc=-0.024, current_price=102.5,
        )
        return value

    broker.snapshot = snapshot
    engine = OneShotEngine(store, broker, None, bundle,
                           require_watchdog=False, check_disk_config=False)
    context = engine.context()
    detail = context["account"]["positions"]["CRWV"]
    assert context["account"]["quantities"]["CRWV"] == -5
    assert detail["direction"] == "short"
    assert detail["short_qty"] == 5
    assert detail["unrealized_pl"] == -12.5
    store.close()
