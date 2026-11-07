"""Isolated behavioral contract for the proposed one-shot engine.

Every broker is synthetic.  The production worker never imports this engine.
"""

from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from pydantic import ValidationError

from trading_bot.decision_schema import OrderIntent
from trading_bot.deepseek_client import PROMPT_FILES
from trading_bot.dry_run import FakeBroker
from trading_bot.one_shot_engine import OneShotDecision, OneShotEngine, parse_one_shot_decision
from trading_bot.settings import ROOT
from trading_bot.storage import Store


def command(context, *, action="BUY", qty=3, price="99.50", decision_id="decision-1",
            cancel=False, additional=False, symbol="CRWV"):
    return OneShotDecision(
        decision_id=decision_id, snapshot_id=context["snapshot_id"],
        action="PLACE_ORDER", reason="Synthetic scenario, not investment advice",
        order_intent=OrderIntent(action=action, symbol=symbol, qty=qty, limit_price=price),
        cancel_conflicting_orders=cancel,
        intentional_additional_pending=additional,
    )


def no_trade(context):
    return OneShotDecision(
        decision_id="no-trade", snapshot_id=context["snapshot_id"],
        action="NO_TRADE", reason="No new exposure in the synthetic scenario",
    )


def build(store, bundle, clock, broker=None, ai=None, context_provider=None):
    broker = broker if broker is not None else FakeBroker(fill=False)
    engine = OneShotEngine(
        store, broker, ai, bundle, clock=clock, context_provider=context_provider,
    )
    return engine, broker


def test_one_shot_order_is_immediate_and_no_trade_does_not_cancel(store, bundle, clock):
    engine, broker = build(store, bundle, clock)
    snapshot = engine.context()
    first = engine.apply_decision(command(snapshot), snapshot)
    assert first["state"] == "ACCEPTED"
    assert broker.submissions == 1
    updated = engine.context()
    assert updated["account"]["open_orders"][0]["client_order_id"] == first["cid"]
    assert updated["account"]["reserved_buy_cash"] == pytest.approx(3 * 99.50)
    assert updated["account"]["quantities"] == {}
    assert engine.apply_decision(no_trade(updated), updated)["status"] == "NO_TRADE"
    assert broker.submissions == 1 and broker.cancel_calls == []
    broker.advance_fill(first["cid"])
    final = engine.context()
    assert final["account"]["quantities"]["CRWV"] == 3
    assert final["account"]["open_orders"] == []


def test_identical_decision_id_is_idempotent(store, bundle, clock):
    engine, broker = build(store, bundle, clock)
    context = engine.context()
    first = engine.apply_decision(command(context), context)
    assert first["state"] == "ACCEPTED"
    same = engine.context()
    again = engine.apply_decision(command(same), same)
    assert again["status"] == "DUPLICATE"
    assert again["cid"] == first["cid"]
    assert broker.submissions == 1


def test_decision_reservation_is_atomic_across_store_connections(tmp_path, clock):
    path = tmp_path / "shared.sqlite"
    first = Store(path, clock=clock, instance_id="first")
    second = Store(path, clock=clock, instance_id="second")
    barrier = Barrier(2)
    try:
        def reserve(store):
            barrier.wait()
            return store.reserve_one_shot_decision("one-id", "same-fingerprint", "PLACE_ORDER")

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(reserve, (first, second)))
        assert sorted(outcomes) == ["DUPLICATE", "NEW"]
        assert second.reserve_one_shot_decision("one-id", "changed", "PLACE_ORDER") == "CONFLICT"
    finally:
        first.close()
        second.close()


def test_no_trade_id_cannot_later_become_an_order(store, bundle, clock):
    engine, broker = build(store, bundle, clock)
    context = engine.context()
    assert engine.apply_decision(no_trade(context), context)["status"] == "NO_TRADE"
    fresh = engine.context()
    result = engine.apply_decision(command(fresh, decision_id="no-trade"), fresh)
    assert result == {"status": "BLOCKED", "reason": "DECISION_ID_CONFLICT"}
    assert broker.submissions == 0


def test_same_decision_id_with_changed_price_is_rejected(store, bundle, clock):
    engine, broker = build(store, bundle, clock)
    old = engine.context()
    engine.apply_decision(command(old), old)
    fresh = engine.context()
    assert engine.apply_decision(command(fresh, price="98.50"), fresh) == {
        "status": "BLOCKED", "reason": "DECISION_ID_CONFLICT"
    }
    assert broker.submissions == 1


def test_same_decision_id_cannot_be_reused_for_another_symbol(store, bundle, clock):
    engine, broker = build(store, bundle, clock)
    first = engine.context()
    assert engine.apply_decision(command(first), first)["state"] == "ACCEPTED"
    fresh = engine.context()
    reused = engine.apply_decision(command(fresh, symbol="NBIS"), fresh)
    assert reused == {"status": "BLOCKED", "reason": "DECISION_ID_CONFLICT"}
    assert broker.submissions == 1


def test_new_decision_cannot_accidentally_duplicate_same_pending_limit_order(store, bundle, clock):
    engine, broker = build(store, bundle, clock)
    before = engine.context()
    engine.apply_decision(command(before), before)
    after = engine.context()
    rejected = engine.apply_decision(command(after, decision_id="another-cycle"), after)
    assert rejected == {"status": "BLOCKED", "reason": "DUPLICATE_PENDING_INTENT"}
    assert broker.submissions == 1
    # A separately identified, intentionally additional order is not silently prohibited.
    after = engine.context()
    intentional = engine.apply_decision(
        command(after, decision_id="deliberate-second-order", additional=True), after,
    )
    assert intentional["state"] == "ACCEPTED" and broker.submissions == 2


def test_partial_fill_closing_position_cancels_remainder_first(store, bundle, clock):
    engine, broker = build(store, bundle, clock)
    context = engine.context()
    buy = engine.apply_decision(command(context, qty=10), context)
    assert buy["state"] == "ACCEPTED"
    broker.orders[buy["cid"]].update(status="partially_filled", filled_qty="3")
    broker.positions["CRWV"] = 3
    snapshot = engine.context()
    assert snapshot["account"]["quantities"]["CRWV"] == 3
    close = engine.apply_decision(
        command(snapshot, action="SELL", qty=3, price="100.50",
                decision_id="exit-partial", cancel=True), snapshot,
    )
    assert close["state"] == "ACCEPTED"
    assert broker.cancel_calls == [buy["cid"]]
    assert store.order_row(buy["cid"])["state"] == "CANCELED"
    assert broker.submissions == 2


def test_partial_fill_without_explicit_cancel_stays_blocked(store, bundle, clock):
    engine, broker = build(store, bundle, clock)
    first = engine.context()
    buy = engine.apply_decision(command(first, qty=10), first)
    broker.orders[buy["cid"]].update(status="partially_filled", filled_qty="3")
    broker.positions["CRWV"] = 3
    snapshot = engine.context()
    close = engine.apply_decision(
        command(snapshot, action="SELL", qty=3, price="100.50", decision_id="exit", cancel=False),
        snapshot,
    )
    assert close["state"] == "NOT_SENT"
    assert close["rejected"] == "CONFLICTING_ORDER"
    assert broker.submissions == 1 and broker.cancel_calls == []


def test_fake_position_exit_cannot_cancel_an_unfilled_entry(store, bundle, clock):
    engine, broker = build(store, bundle, clock)
    original = engine.context()
    assert engine.apply_decision(command(original, qty=10), original)["state"] == "ACCEPTED"
    assert broker.positions == {}
    snapshot = engine.context()
    result = engine.apply_decision(
        command(snapshot, action="SELL", qty=1, decision_id="fake-exit", cancel=True), snapshot,
    )
    assert result == {"status": "BLOCKED", "reason": "INSUFFICIENT_POSITION_FOR_EXIT"}
    assert broker.cancel_calls == [] and broker.submissions == 1


def test_unconfirmed_cancel_does_not_send_exit(store, bundle, clock):
    class UnconfirmedCancel(FakeBroker):
        def cancel(self, cid):
            self.cancel_calls.append(cid)  # Broker still says partially_filled.

    engine, broker = build(store, bundle, clock, broker=UnconfirmedCancel(fill=False))
    original = engine.context()
    buy = engine.apply_decision(command(original, qty=10), original)
    broker.orders[buy["cid"]].update(status="partially_filled", filled_qty="3")
    broker.positions["CRWV"] = 3
    snapshot = engine.context()
    result = engine.apply_decision(
        command(snapshot, action="SELL", qty=3, decision_id="exit", cancel=True), snapshot,
    )
    assert result == {"status": "BLOCKED", "reason": "CANCEL_NOT_CONFIRMED"}
    assert broker.submissions == 1


def test_extra_fill_during_cancel_requires_new_position_decision(store, bundle, clock):
    class RaceFill(FakeBroker):
        def cancel(self, cid):
            # Two additional shares fill during the cancellation race; the
            # remaining five are canceled.  This is not an automatic fill().
            self.orders[cid]["filled_qty"] = "5"
            self.positions["CRWV"] = 5
            super().cancel(cid)

    engine, broker = build(store, bundle, clock, broker=RaceFill(fill=False))
    original = engine.context()
    buy = engine.apply_decision(command(original, qty=10), original)
    broker.orders[buy["cid"]].update(status="partially_filled", filled_qty="3")
    broker.positions["CRWV"] = 3
    snapshot = engine.context()
    result = engine.apply_decision(
        command(snapshot, action="SELL", qty=3, decision_id="exit", cancel=True), snapshot,
    )
    assert result == {"status": "BLOCKED", "reason": "POSITION_CHANGED_DURING_CANCEL"}
    assert broker.submissions == 1 and broker.positions["CRWV"] == 5


def test_cancel_exception_freezes_new_orders(store, bundle, clock):
    class FailingCancel(FakeBroker):
        def cancel(self, cid):
            raise TimeoutError("outcome uncertain")

    engine, broker = build(store, bundle, clock, broker=FailingCancel(fill=False))
    original = engine.context()
    buy = engine.apply_decision(command(original, qty=10), original)
    broker.orders[buy["cid"]].update(status="partially_filled", filled_qty="3")
    broker.positions["CRWV"] = 3
    snapshot = engine.context()
    result = engine.apply_decision(
        command(snapshot, action="SELL", qty=3, decision_id="exit", cancel=True), snapshot,
    )
    assert result == {"status": "BLOCKED", "reason": "CANCEL_OUTCOME_UNKNOWN"}
    assert "CANCEL_OUTCOME_UNKNOWN" in store.freeze_reason()
    assert broker.submissions == 1


def test_after_noon_is_read_only_even_with_pending_orders(store, bundle, clock):
    class NeverCallAI:
        def complete(self, *args, **kwargs):
            raise AssertionError("AI decision must not be executed after cutoff")

    engine, broker = build(store, bundle, clock, ai=NeverCallAI())
    before = engine.context()
    buy = engine.apply_decision(command(before), before)
    assert buy["state"] == "ACCEPTED"
    clock.set(datetime(2026, 11, 5, 17, 5, tzinfo=timezone.utc))
    engine.context_provider = lambda _: (_ for _ in ()).throw(
        AssertionError("research provider must not run after noon")
    )
    outcome = engine.cycle()
    assert outcome["status"] == "OBSERVE_ONLY"
    assert engine.apply_decision(command(outcome["context"], decision_id="late"), outcome["context"])["status"] == "OBSERVE_ONLY"
    assert broker.submissions == 1 and broker.cancel_calls == []
    assert store.order_row(buy["cid"])["state"] == "ACCEPTED"
    broker.advance_fill(buy["cid"])
    observed = engine.cycle()
    assert observed["status"] == "OBSERVE_ONLY"
    assert observed["context"]["account"]["quantities"]["CRWV"] == 3


def test_at_exact_cutoff_no_new_order_or_cancellation(store, bundle, clock):
    engine, broker = build(store, bundle, clock)
    original = engine.context()
    buy = engine.apply_decision(command(original, qty=10), original)
    broker.orders[buy["cid"]].update(status="partially_filled", filled_qty="3")
    broker.positions["CRWV"] = 3
    clock.set(datetime(2026, 11, 5, 16, 59, 30, tzinfo=timezone.utc))
    later = engine.context()
    outcome = engine.apply_decision(
        command(later, action="SELL", qty=3, decision_id="too-late", cancel=True), later,
    )
    assert outcome == {"status": "OBSERVE_ONLY", "reason": "OUTSIDE_SUBMISSION_WINDOW"}
    assert broker.submissions == 1 and broker.cancel_calls == []


def test_ai_response_finishing_after_cutoff_never_dispatches(store, bundle, clock):
    class SlowAI:
        def complete(self, mode, context, parser):
            clock.set(datetime(2026, 11, 5, 16, 59, 30, tzinfo=timezone.utc))
            return parser(command(context).model_dump_json()), {"model": "offline-fixture"}

    clock.set(datetime(2026, 11, 5, 16, 59, 20, tzinfo=timezone.utc))
    engine, broker = build(store, bundle, clock, ai=SlowAI())
    result = engine.cycle()
    assert result == {"status": "OBSERVE_ONLY", "reason": "OUTSIDE_SUBMISSION_WINDOW"}
    assert broker.submissions == 0 and broker.cancel_calls == []


def test_stale_snapshot_or_changed_account_does_not_trade(store, bundle, clock):
    engine, broker = build(store, bundle, clock)
    original = engine.context()
    broker.positions["CRWV"] = 2
    result = engine.apply_decision(command(original), original)
    assert result == {"status": "BLOCKED", "reason": "ACCOUNT_CHANGED_SINCE_AI_DECISION"}
    assert broker.submissions == 0
    broker.positions.clear()
    clock.advance(91)
    result = engine.apply_decision(command(original), original)
    assert result == {"status": "BLOCKED", "reason": "STALE_DECISION_CONTEXT"}
    assert broker.submissions == 0


def test_strict_schema_rejects_no_trade_with_order_and_exit_cancel_on_buy(store, bundle, clock):
    context = build(store, bundle, clock)[0].context()
    payload = command(context).model_dump(mode="json")
    payload["action"] = "NO_TRADE"
    with pytest.raises(ValidationError):
        OneShotDecision.model_validate(payload)
    payload = command(context).model_dump(mode="json")
    payload["cancel_conflicting_orders"] = True
    with pytest.raises(ValidationError):
        OneShotDecision.model_validate(payload)
    with pytest.raises(ValidationError):
        parse_one_shot_decision('{"mode":"ONE_SHOT_DECISION","action":"BUY","extra":1}')


def test_outside_stock_pool_is_rejected_by_existing_order_service(store, bundle, clock):
    engine, broker = build(store, bundle, clock)
    context = engine.context()
    rejected = engine.apply_decision(command(context, symbol="NOTINPOOL"), context)
    assert rejected["state"] == "NOT_SENT"
    assert rejected["rejected"] == "OUTSIDE_WATCHLIST"
    assert broker.submissions == 0


def test_non_fake_broker_is_forbidden(store, bundle, clock):
    with pytest.raises(RuntimeError, match="OFFLINE-ONLY"):
        OneShotEngine(store, object(), None, bundle, clock=clock)


class ScriptedOneShotAI:
    def __init__(self, decisions):
        self.decisions = list(decisions)
        self.calls = 0

    def complete(self, mode, context, parser):
        assert mode == "ONE_SHOT_DECISION"
        self.calls += 1
        choice = self.decisions.pop(0)
        result = choice(context)
        return parser(result.model_dump_json()), {"model": "offline-fixture"}


def test_full_ai_buy_fill_then_position_review_sell(store, bundle, clock):
    def buy(context):
        return command(context, decision_id="open")

    def sell(context):
        assert context["account"]["quantities"]["CRWV"] == 3
        return command(context, action="SELL", qty=3, price="100.50", decision_id="close")

    ai = ScriptedOneShotAI([buy, sell])
    engine, broker = build(store, bundle, clock, ai=ai, broker=FakeBroker(fill=True))
    first = engine.cycle()
    assert first["state"] == "FILLED" and broker.positions["CRWV"] == 3
    second = engine.cycle()
    assert second["state"] == "FILLED" and broker.positions["CRWV"] == 0
    assert broker.submissions == ai.calls == 2


def test_cycle_exposes_all_state_to_ai(store, bundle, clock):
    ai = ScriptedOneShotAI([no_trade])
    engine, broker = build(
        store, bundle, clock, ai=ai,
        context_provider=lambda context: {"synthetic_news": ["not a real source"]},
    )
    outcome = engine.cycle()
    assert outcome["status"] == "NO_TRADE"
    recorded = store.recent_events(("one_shot_decision",), 1)[0]["payload"]
    assert recorded["action"] == "NO_TRADE"
    context = engine.context()
    assert "open_orders" in context["account"]
    assert "positions" in context["account"]
    assert "reserved_buy_cash" in context["account"]
    assert "synthetic_news" in context["research"]
    assert context["strategy_guidance"]["authority"] == "advisory, not mandatory trade filters"
    assert context["execution_contract"]["time_in_force"] == "day"


def test_experimental_prompt_keeps_strategy_advisory_and_orders_one_shot():
    prompt = (ROOT / "prompts" / PROMPT_FILES["ONE_SHOT_DECISION"]).read_text(encoding="utf-8")
    assert "NOT mandatory filters" in prompt
    assert "NO_TRADE does not cancel it" in prompt
    assert "cancel_conflicting_orders" in prompt
    assert "snapshot_id" in prompt
