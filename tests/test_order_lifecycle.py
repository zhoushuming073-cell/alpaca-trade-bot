import threading
import time
from datetime import datetime, timezone

import pytest

from trading_bot.dry_run import FakeBroker
from trading_bot.execution import OrderService
from trading_bot.order_state import OrderState
from trading_bot.safety import PreDispatchRejected
from trading_bot.storage import Store


def intent(**overrides):
    value = {
        "symbol": "CRWV", "side": "buy", "qty": 3, "limit_price": 100.05,
        "order_type": "limit", "time_in_force": "day", "extended_hours": False,
        "stop_policy": "NONE", "stop_loss": None,
    }
    value.update(overrides)
    return value


def submit(store, broker, bundle, clock, session, **overrides):
    service = OrderService(store, broker, bundle.execution, clock=clock)
    return service.submit(
        intent(**overrides), plan_id="plan-1", trigger_id="trigger-1",
        day=session.trading_day, session=session,
        account=broker.snapshot(), quote=broker.quote("CRWV"),
        symbols=bundle.symbols, risk=bundle.risk, buy_attempts=0,
    )


def test_pre_dispatch_rejection_is_not_sent_without_lookup(store, bundle, clock, session):
    broker = FakeBroker(prepare_error="quote expired before dispatch")
    result = submit(store, broker, bundle, clock, session)
    assert result["state"] == OrderState.NOT_SENT.value
    assert broker.submissions == 0
    assert broker.lookup_calls == 0
    assert store.safety_reason() == ""
    assert store.pending() == []
    row = store.order_row(result["cid"])
    assert row["state"] == OrderState.NOT_SENT.value
    assert not store.dispatched(result["cid"])
    assert store.order_row(result["cid"])["reason"].startswith("PreDispatchRejected")


def test_watchdog_reconcile_ignores_pre_dispatch_reservation(store, bundle):
    broker = FakeBroker()
    store.reserve_order("aip-local", "2026-11-05", intent(), owner="submitter")
    assert store.claim_order("aip-local", "submitter")
    before = store.order_row("aip-local")["state"]
    OrderService(store, broker, bundle.execution).reconcile()
    assert store.order_row("aip-local")["state"] == before == OrderState.VALIDATING.value
    assert broker.lookup_calls == 0
    assert store.safety_reason() == ""


def test_watchdog_cannot_poison_inflight_post_dispatch(tmp_path, bundle, clock, session, monkeypatch):
    path = tmp_path / "race.sqlite"
    store = Store(path, clock=clock, instance_id="engine")
    broker = FakeBroker()
    broker._entered = threading.Event()
    broker._proceed = threading.Event()

    def slow_dispatch(intent_value, cid):
        broker._entered.set()
        broker._proceed.wait(5)
        return FakeBroker.dispatch(broker, intent_value, cid)

    broker.dispatch = slow_dispatch
    service = OrderService(store, broker, bundle.execution, clock=clock)
    result = {}

    def run_submit():
        result.update(service.submit(
            intent(), plan_id="plan-race", trigger_id="trigger-race",
            day=session.trading_day, session=session, account=broker.snapshot(),
            quote=broker.quote("CRWV"), symbols=bundle.symbols,
            risk=bundle.risk, buy_attempts=0,
        ))

    thread = threading.Thread(target=run_submit)
    thread.start()
    assert broker._entered.wait(5)
    # The order is DISPATCH_INTENT_RECORDED now and may have been sent; a
    # concurrent watchdog must not delete/reclassify it.
    watchdog_store = Store(path, clock=clock, instance_id="watchdog")
    OrderService(watchdog_store, broker, bundle.execution, clock=clock).reconcile()
    # Within the submission grace window a 404 must not freeze the account.
    assert watchdog_store.safety_reason() == ""
    broker._proceed.set()
    thread.join(5)
    assert result["state"] == OrderState.FILLED.value
    assert broker.submissions == 1
    assert watchdog_store.safety_reason() == ""
    watchdog_store.close()
    store.close()


def test_watchdog_freeze_after_submission_grace(store, bundle, clock):
    store.reserve_order("aip-grace", "2026-11-05", intent(), owner="submitter")
    assert store.claim_order("aip-grace", "submitter")
    assert store.transition("aip-grace", OrderState.READY_TO_DISPATCH,
                            expected=OrderState.VALIDATING, owner="submitter")
    assert store.record_dispatch_intent("aip-grace", "submitter")
    broker = FakeBroker()
    service = OrderService(store, broker, bundle.execution, clock=clock)
    service.reconcile()
    assert store.order_row("aip-grace")["state"] == OrderState.DISPATCH_INTENT_RECORDED.value
    assert store.safety_reason() == ""
    clock.advance(31)
    service.reconcile()
    assert store.order_row("aip-grace")["state"] == OrderState.SUBMISSION_OUTCOME_UNKNOWN.value
    assert store.safety_reason()


def test_startup_crash_before_dispatch_is_recovered_not_sent(tmp_path, bundle, clock):
    path = tmp_path / "crash.sqlite"
    first = Store(path, clock=clock, instance_id="old-process")
    first.reserve_order("aip-crash", "2026-11-05", intent(), owner="old-process")
    assert first.claim_order("aip-crash", "old-process")
    first.close()
    second = Store(path, clock=clock, instance_id="new-process")
    recovered = second.recover_stale_leases()
    assert "aip-crash" in recovered
    assert second.order_row("aip-crash")["state"] == OrderState.NOT_SENT.value
    assert not second.dispatched("aip-crash")
    second.close()


def test_post_dispatch_crash_is_never_marked_not_sent(tmp_path, bundle, clock, session):
    path = tmp_path / "post.sqlite"
    store = Store(path, clock=clock, instance_id="old-process")
    store.reserve_order("aip-post", "2026-11-05", intent(), owner="old-process")
    assert store.claim_order("aip-post", "old-process")
    assert store.transition("aip-post", OrderState.READY_TO_DISPATCH,
                            expected=OrderState.VALIDATING, owner="old-process")
    assert store.record_dispatch_intent("aip-post", "old-process")
    store.close()
    second = Store(path, clock=clock, instance_id="new-process")
    second.recover_stale_leases()
    assert second.order_row("aip-post")["state"] == OrderState.DISPATCH_INTENT_RECORDED.value
    broker = FakeBroker()
    broker.orders = {}  # 404: cannot prove it was not sent
    clock.advance(31)
    OrderService(second, broker, bundle.execution, clock=clock).reconcile()
    assert second.order_row("aip-post")["state"] == OrderState.SUBMISSION_OUTCOME_UNKNOWN.value
    assert second.safety_reason()
    second.close()


def test_post_dispatch_timeout_but_broker_accepted_is_reconciled(store, bundle, clock, session):
    broker = FakeBroker(dispatch_timeout=True)
    result = submit(store, broker, bundle, clock, session)
    assert result["state"] == OrderState.FILLED.value
    assert broker.submissions == 1
    assert store.safety_reason() == ""
    assert store.order_row(result["cid"])["state"] == OrderState.FILLED.value


def test_post_dispatch_timeout_without_record_stays_unknown(store, bundle, clock, session):
    class Lost(FakeBroker):
        def dispatch(self, intent_value, cid):
            raise TimeoutError("lost before broker record")

    broker = Lost(fill=True)
    result = submit(store, broker, bundle, clock, session)
    assert result["state"] == OrderState.SUBMISSION_OUTCOME_UNKNOWN.value
    assert result.get("unknown")
    assert broker.lookup_calls == 1
    assert store.safety_reason()
    row = store.order_row(result["cid"])
    assert row["state"] != OrderState.NOT_SENT.value
    assert store.dispatched(result["cid"])


def test_out_of_order_poll_cannot_erase_fill(store):
    store.reserve_order("aip-fill", "2026-11-05", intent())
    store.apply_broker_order("aip-fill", {"status": "partially_filled", "filled_qty": "2"})
    store.apply_broker_order("aip-fill", {"status": "canceled", "filled_qty": "2"})
    store.apply_broker_order("aip-fill", {"status": "new", "filled_qty": "0"})
    row = store.order_row("aip-fill")
    assert row["state"] == OrderState.CANCELED.value
    assert list(store.db.execute("SELECT count(*) FROM fills"))[0][0] == 1


def test_local_terminal_broker_active_is_conflict(store):
    store.reserve_order("aip-conflict", "2026-11-05", intent(), owner="owner")
    assert store.claim_order("aip-conflict", "owner")
    assert store.transition("aip-conflict", OrderState.READY_TO_DISPATCH,
                            expected=OrderState.VALIDATING, owner="owner")
    assert store.record_dispatch_intent("aip-conflict", "owner")
    store.apply_broker_order("aip-conflict", {"status": "canceled", "filled_qty": "0"})
    store.apply_broker_order("aip-conflict", {"status": "new", "filled_qty": "0"})
    assert store.order_row("aip-conflict")["state"] == OrderState.RECONCILIATION_REQUIRED.value
    assert store.safety_reason()


def test_duplicate_submit_same_trigger_posts_once(store, bundle, clock, session):
    broker = FakeBroker()
    first = submit(store, broker, bundle, clock, session)
    second = submit(store, broker, bundle, clock, session)
    assert first["cid"] == second["cid"]
    assert broker.submissions == 1
    assert store.count("2026-11-05") == 1


def test_concurrent_submit_posts_once(store, bundle, clock, session):
    broker = FakeBroker()
    barrier = threading.Barrier(2)
    results = []

    def run():
        barrier.wait(5)
        results.append(submit(store, broker, bundle, clock, session))

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert len(results) == 2
    assert {r["cid"] for r in results} == {results[0]["cid"]}
    assert broker.submissions == 1
    assert store.count("2026-11-05") == 1


def test_not_sent_can_be_rearmed_and_retried(store, bundle, clock, session):
    broker = FakeBroker(prepare_error="first attempt rejected")
    first = submit(store, broker, bundle, clock, session)
    assert first["state"] == OrderState.NOT_SENT.value
    store.rearm_trigger("plan-1", "trigger-1")
    broker.prepare_error = None
    second = submit(store, broker, bundle, clock, session, qty=3)
    # Same attempt number intentionally re-uses the cid; a real retry must use
    # the next attempt id.
    assert second["state"] == OrderState.NOT_SENT.value
    third_service = OrderService(store, broker, bundle.execution, clock=clock)
    third = third_service.submit(
        intent(), plan_id="plan-1", trigger_id="trigger-1", day=session.trading_day,
        session=session, account=broker.snapshot(), quote=broker.quote("CRWV"),
        symbols=bundle.symbols, risk=bundle.risk, buy_attempts=0, attempt=1,
    )
    assert third["state"] == OrderState.FILLED.value
    assert broker.submissions == 1


def test_pause_during_slow_preflight_prevents_post(store, bundle, clock, session):
    class Pauses(FakeBroker):
        def prepare(self, intent_value, cid, session=None, account=None):
            store.pause(True)
            return self.snapshot()

    broker = Pauses()
    result = submit(store, broker, bundle, clock, session)
    assert result["state"] == OrderState.NOT_SENT.value
    assert broker.submissions == 0
    assert not store.dispatched(result["cid"])


def test_config_change_during_slow_preflight_prevents_post(store, bundle, clock, session):
    class ChangesConfig(FakeBroker):
        def prepare(self, intent_value, cid, session=None, account=None):
            store.set_active_config({
                "symbols": list(bundle.symbols),
                "hashes": {"combined": "old-config"},
                "version": "old",
                "execution": bundle.execution.model_dump(mode="json"),
            })
            return self.snapshot()
    broker = ChangesConfig()
    result = submit(store, broker, bundle, clock, session)
    assert result["state"] == OrderState.NOT_SENT.value
    assert broker.submissions == 0


def test_safety_freeze_during_slow_preflight_prevents_post(store, bundle, clock, session):
    class Freezes(FakeBroker):
        def prepare(self, intent_value, cid, session=None, account=None):
            store.block("ORDER_UNKNOWN: simulated concurrent freeze")
            return self.snapshot()

    broker = Freezes()
    result = submit(store, broker, bundle, clock, session)
    assert result["state"] == OrderState.NOT_SENT.value
    assert broker.submissions == 0
    assert "ORDER_UNKNOWN" in store.safety_reason()


def test_slow_preflight_crossing_cutoff_is_not_sent(store, bundle, clock, session):
    class Slow(FakeBroker):
        def prepare(self, intent_value, cid, session=None, account=None):
            clock.advance(9000)
            return self.snapshot()

    broker = Slow()
    result = submit(store, broker, bundle, clock, session)
    assert result["state"] == OrderState.NOT_SENT.value
    assert broker.submissions == 0
    assert broker.lookup_calls == 0
    assert not store.dispatched(result["cid"])


def test_broker_stop_intent_is_rejected_as_a_whole(store, bundle, clock, session):
    broker = FakeBroker()
    result = submit(
        store, broker, bundle, clock, session,
        stop_policy="BROKER_STOP", stop_loss={"stop_price": 95},
    )
    assert result["state"] == OrderState.NOT_SENT.value
    assert result.get("rejected") == "BROKER_STOP_UNSUPPORTED"
    assert broker.submissions == 0
    assert store.order_row(result["cid"])["stop_status"] == "REQUESTED"


def test_broker_stop_without_leg_freezes_even_on_partial_fill(store):
    request = intent(stop_policy="BROKER_STOP", stop_loss={"stop_price": 95})
    store.reserve_order("aip-stop-missing", "2026-11-05", request, owner="owner")
    assert store.claim_order("aip-stop-missing", "owner")
    assert store.transition("aip-stop-missing", OrderState.READY_TO_DISPATCH,
                            expected=OrderState.VALIDATING, owner="owner")
    assert store.record_dispatch_intent("aip-stop-missing", "owner")
    store.apply_broker_order("aip-stop-missing", {
        "id": "b1", "client_order_id": "aip-stop-missing", "symbol": "CRWV",
        "side": "buy", "qty": "3", "filled_qty": "1", "status": "partially_filled",
        "type": "limit", "order_class": "oto",
        "legs": [{"type": "take_profit", "side": "sell", "qty": "3", "status": "new"}],
    })
    row = store.order_row("aip-stop-missing")
    assert row["state"] == OrderState.PARTIALLY_FILLED.value
    assert row["stop_status"] == "FAILED"
    assert "stop leg" in store.safety_reason() or store.safety_reason()


def test_partial_fill_then_cancel_keeps_fill(store):
    store.reserve_order("aip-partial", "2026-11-05", intent(), owner="owner")
    assert store.claim_order("aip-partial", "owner")
    assert store.transition("aip-partial", OrderState.READY_TO_DISPATCH,
                            expected=OrderState.VALIDATING, owner="owner")
    assert store.record_dispatch_intent("aip-partial", "owner")
    store.apply_broker_order("aip-partial", {"status": "partially_filled", "filled_qty": "2"})
    store.apply_broker_order("aip-partial", {"status": "canceled", "filled_qty": "2"})
    row = store.order_row("aip-partial")
    assert row["state"] == OrderState.CANCELED.value
    assert list(store.db.execute("SELECT cumulative_qty FROM fills WHERE cid='aip-partial'"))[0][0] == "2"


def test_operator_resolution_of_unknown_then_acknowledge(store, bundle, clock, session):
    store.reserve_order("aip-unknown", "2026-11-05", intent(), owner="owner")
    assert store.claim_order("aip-unknown", "owner")
    assert store.transition("aip-unknown", OrderState.READY_TO_DISPATCH,
                            expected=OrderState.VALIDATING, owner="owner")
    assert store.record_dispatch_intent("aip-unknown", "owner")
    clock.advance(31)
    OrderService(store, FakeBroker(), bundle.execution, clock=clock).reconcile()
    assert store.has_unknown_orders()
    assert store.safety_reason()
    with pytest.raises(ValueError):
        store.acknowledge()
    assert store.resolve_unknown_not_sent("aip-unknown", "operator checked Alpaca by client_order_id")
    assert store.order_row("aip-unknown")["state"] == OrderState.NOT_SENT.value
    assert not store.has_unknown_orders()
    store.acknowledge()
    assert store.safety_reason() == ""
