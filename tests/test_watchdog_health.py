import sqlite3
from datetime import datetime, timezone

import pytest

import trading_bot.watchdog as watchdog_module
from trading_bot.dry_run import FakeBroker
from trading_bot.order_state import OrderState
from trading_bot.storage import Store
from trading_bot.watchdog import CutoffWatchdog


def accepted_order(store, *, cid="aip-watchdog"):
    store.reserve_order(cid, "2026-11-05", {
        "symbol": "CRWV", "side": "buy", "qty": 1, "limit_price": 100.0,
    }, owner="owner")
    assert store.claim_order(cid, "owner")
    assert store.transition(cid, OrderState.READY_TO_DISPATCH,
                            expected=OrderState.VALIDATING, owner="owner")
    assert store.record_dispatch_intent(cid, "owner")
    store.apply_broker_order(cid, {
        "id": "broker-1", "client_order_id": cid, "symbol": "CRWV",
        "side": "buy", "qty": "1", "filled_qty": "0", "status": "new", "type": "limit",
    })
    return cid


def test_watchdog_heartbeats_and_never_cancels_day_order(store, bundle, clock):
    broker = FakeBroker(fill=False)
    cid = accepted_order(store)
    broker.orders[cid] = {
        "id": "broker-1", "client_order_id": cid, "symbol": "CRWV", "side": "buy",
        "qty": "1", "filled_qty": "0", "status": "new", "type": "limit",
    }
    worker = CutoffWatchdog(store.path, broker, bundle.execution, clock=clock, interval_seconds=2)
    steps = iter([False, True])
    worker.stop_event.wait = lambda seconds: next(steps)
    worker.run()
    assert broker.cancel_calls == []
    check = Store(store.path, clock=clock, role="check")
    health = check.health("watchdog")
    assert health["alive"] is False
    assert health["last_success"] is not None
    assert check.safety_reason() == ""
    check.close()


def test_watchdog_isolates_iteration_failures_and_freezes(store, bundle, clock, monkeypatch):
    class Broken:
        def __init__(self, *args, **kwargs):
            pass

        def reconcile(self, **kwargs):
            raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(watchdog_module, "OrderService", Broken)
    worker = CutoffWatchdog(store.path, FakeBroker(), bundle.execution, clock=clock, interval_seconds=2)
    steps = iter([False, False, False, True])
    worker.stop_event.wait = lambda seconds: next(steps)
    worker.run()
    check = Store(store.path, clock=clock, role="check")
    health = check.health("watchdog")
    assert health["consecutive_failures"] == 3
    assert health["alive"] is False
    assert "WATCHDOG_UNHEALTHY" in check.safety_reason()
    check.close()


def test_watchdog_marks_itself_dead_even_without_failures(store, bundle, clock):
    worker = CutoffWatchdog(store.path, FakeBroker(), bundle.execution, clock=clock, interval_seconds=2)
    steps = iter([True])  # stop immediately
    worker.stop_event.wait = lambda seconds: next(steps)
    worker.run()
    check = Store(store.path, clock=clock, role="check")
    assert check.health("watchdog")["alive"] is False
    check.close()
