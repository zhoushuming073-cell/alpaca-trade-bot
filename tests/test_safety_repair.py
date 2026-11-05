import json

import pytest

from trading_bot.network import read_retry
from trading_bot.order_state import OrderState
from trading_bot.safety import validate_order
from trading_bot.storage import Store


def test_credentials_redacted_in_events_errors(store, monkeypatch, caplog):
    monkeypatch.setenv("ALPACA_API_SECRET", "test-private-secret-999")
    store.error("network", RuntimeError("Authorization: Bearer test-private-secret-999"),
                ticker="TEST", cycle="cycle")
    store.event("ai_raw", {"api_key": "another-secret",
                           "nested": {"value": "test-private-secret-999"}})
    text = str(store.db.execute("SELECT payload FROM events").fetchall()) + caplog.text
    assert "test-private-secret-999" not in text
    assert "another-secret" not in text
    assert "[REDACTED]" in text


def test_acknowledgement_requires_no_unknown_orders(store):
    store.import_broker_order("aip-unknown", {
        "id": "broker-1", "client_order_id": "aip-unknown", "symbol": "CRWV",
        "side": "buy", "qty": "1", "filled_qty": "0", "status": "new",
    })
    with pytest.raises(ValueError):
        store.acknowledge()
    assert store.paused()
    assert store.safety_reason()


def test_read_retries_bounded(monkeypatch):
    import requests

    monkeypatch.setattr("trading_bot.network.time.sleep", lambda seconds: None)
    attempts, events = [], []

    def offline():
        attempts.append(1)
        raise requests.Timeout("read timeout")

    with pytest.raises(requests.Timeout) as failure:
        read_retry(offline, lambda *args: events.append(args))
    assert len(attempts) == 3
    assert len(events) == 2
    assert failure.value._bot_retries == 2


def test_direct_order_bypass_is_forbidden():
    from trading_bot.broker import build_broker

    broker = build_broker({"ALPACA_API_KEY": "fake", "ALPACA_API_SECRET": "fake"},
                          start_orders_thread=False)
    with pytest.raises(RuntimeError, match="bypass"):
        broker.api.post("/orders", {
            "symbol": "TEST", "side": "buy", "qty": 1, "type": "limit",
            "limit_price": 100, "time_in_force": "day", "client_order_id": "cid",
        })


@pytest.mark.parametrize("change", [
    {"qty": 1.5}, {"qty": 0}, {"time_in_force": "ioc"}, {"type": "market"},
    {"notional": 10}, {"extended_hours": True},
])
def test_low_level_order_validation_rejects_bad_contract(change):
    order = {
        "symbol": "TEST", "side": "buy", "qty": 1, "type": "limit",
        "limit_price": 100, "time_in_force": "day",
    }
    order.update(change)
    with pytest.raises(ValueError):
        validate_order(order)


def test_startup_recovery_marks_local_only_reservation_not_sent(tmp_path):
    path = tmp_path / "recovery.sqlite"
    first = Store(path, instance_id="old")
    first.reserve_order("aip-local", "2026-11-05", {
        "symbol": "CRWV", "side": "buy", "qty": 1, "limit_price": 100.0,
    }, owner="old")
    assert first.claim_order("aip-local", "old")
    first.close()
    second = Store(path, instance_id="new")
    assert "aip-local" in second.recover_stale_leases()
    assert second.order_row("aip-local")["state"] == OrderState.NOT_SENT.value
    assert not second.dispatched("aip-local")
    second.close()
