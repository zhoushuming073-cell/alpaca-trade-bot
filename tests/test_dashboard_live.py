from datetime import datetime, timezone

import pytest

from trading_bot.dashboard import (
    LiveAccountFeed,
    ORIGIN,
    create_app,
    validate_account_snapshot,
)
from trading_bot.order_state import OrderState
from trading_bot.storage import Store


def account_snapshot():
    now = datetime.now(timezone.utc).isoformat()
    return {
        "equity": 101234.56,
        "last_equity": 100000.0,
        "portfolio_value": 101234.56,
        "cash": 50000.0,
        "buying_power": 150000.0,
        "non_margin_cash": 50000.0,
        "reserved_buy_cash": 0.0,
        "available_non_margin_cash": 50000.0,
        "long_market_value": 51234.56,
        "short_market_value": 0.0,
        "initial_margin": 0.0,
        "maintenance_margin": 0.0,
        "daytrade_count": 0,
        "currency": "USD",
        "account_status": "ACTIVE",
        "blocked": False,
        "positions": {"CRWV": 51234.56},
        "position_details": {
            "CRWV": {
                "symbol": "CRWV", "qty": 100.0, "qty_available": 100.0,
                "market_value": 51234.56, "avg_entry_price": 480.0,
                "unrealized_pl": 3234.56, "unrealized_plpc": 0.0674,
                "current_price": 512.3456, "change_today": 0.01, "side": "long",
            }
        },
        "quantities": {"CRWV": 100.0},
        "available_quantities": {"CRWV": 100.0},
        "open_orders": [{
            "id": "broker-1", "client_order_id": "aip-day", "symbol": "CRWV",
            "side": "buy", "qty": "1", "filled_qty": "0", "limit_price": "500",
            "status": "new", "type": "limit", "time_in_force": "day",
        }],
        "recent_orders": [{
            "id": "broker-old", "client_order_id": "old-real-order", "symbol": "IREN",
            "side": "buy", "qty": "100", "filled_qty": "100", "status": "filled",
            "type": "market", "time_in_force": "day",
        }],
        "asof": now,
        "ts": now,
    }


class FakeFeed:
    def __init__(self, account, *, stale=False, error=None):
        self.account = account
        self.stale = stale
        self.error = error

    def get(self):
        return {
            "account": self.account,
            "fetched_at": self.account["asof"],
            "age_seconds": 0 if not self.stale else 500,
            "stale": self.stale,
            "error": self.error,
            "last_attempt": self.account["asof"],
        }


def client(tmp_path, monkeypatch, feed):
    import trading_bot.dashboard as dashboard

    monkeypatch.setattr(dashboard, "worker_processes", lambda: [])
    monkeypatch.setattr(dashboard, "start_worker", lambda: None)
    monkeypatch.setattr(dashboard, "terminate_workers", lambda *args, **kwargs: None)
    return create_app(tmp_path / "web.sqlite", account_feed=feed).test_client()


def local_order(store, cid="aip-day"):
    store.reserve_order(cid, "2026-11-05", {
        "symbol": "CRWV", "side": "buy", "qty": 1, "limit_price": 500.0,
    }, owner="owner")
    assert store.claim_order(cid, "owner")
    assert store.transition(cid, OrderState.READY_TO_DISPATCH,
                            expected=OrderState.VALIDATING, owner="owner")
    assert store.record_dispatch_intent(cid, "owner")
    store.apply_broker_order(cid, {
        "id": "broker-1", "client_order_id": cid, "symbol": "CRWV",
        "side": "buy", "qty": "1", "filled_qty": "0", "status": "new",
        "type": "limit",
    })


def test_live_account_fields_and_position_details(tmp_path, monkeypatch):
    feed = FakeFeed(account_snapshot())
    c = client(tmp_path, monkeypatch, feed)
    data = c.get("/api/status", base_url=ORIGIN).get_json()
    assert data["account_source"] == "ALPACA PAPER 实时"
    assert data["account_stale"] is False
    assert data["account_error"] is None
    assert data["account"]["equity"] == 101234.56
    assert data["account"]["cash"] == 50000.0
    assert data["account"]["buying_power"] == 150000.0
    assert data["account"]["available_non_margin_cash"] == 50000.0
    detail = data["account"]["position_details"]["CRWV"]
    assert detail["qty"] == 100.0
    assert detail["avg_entry_price"] == 480.0
    assert detail["unrealized_pl"] == 3234.56
    assert data["daily_pnl"] == pytest.approx(1234.56)


def test_live_broker_orders_merge_local_state(tmp_path, monkeypatch):
    store = Store(tmp_path / "web.sqlite")
    local_order(store)
    store.close()
    feed = FakeFeed(account_snapshot())
    c = client(tmp_path, monkeypatch, feed)
    data = c.get("/api/status", base_url=ORIGIN).get_json()
    rows = [row for row in data["orders"] if row.get("cid") == "aip-day"]
    assert len(rows) == 1
    assert rows[0]["source"] == "ALPACA_OPEN"
    assert rows[0]["status"] == "new"
    assert rows[0]["local_state"] == OrderState.ACCEPTED.value
    assert rows[0]["state"] == OrderState.ACCEPTED.value


def test_recent_closed_order_is_visible_with_broker_status(tmp_path, monkeypatch):
    feed = FakeFeed(account_snapshot())
    c = client(tmp_path, monkeypatch, feed)
    data = c.get("/api/status", base_url=ORIGIN).get_json()
    rows = [row for row in data["orders"] if row.get("cid") == "old-real-order"]
    assert len(rows) == 1
    assert rows[0]["source"] == "ALPACA_RECENT"
    assert rows[0]["status"] == "filled"
    assert rows[0]["state"] == "FILLED"
    assert rows[0]["qty"] == "100"


def test_stale_feed_keeps_last_real_values_and_marks_error(tmp_path, monkeypatch):
    feed = FakeFeed(account_snapshot(), stale=True, error="TimeoutError: read timeout")
    c = client(tmp_path, monkeypatch, feed)
    data = c.get("/api/status", base_url=ORIGIN).get_json()
    assert data["account_stale"] is True
    assert data["account_source"] == "ALPACA PAPER 最近成功快照"
    assert data["account_error"] == "TimeoutError: read timeout"
    assert data["account"]["equity"] == 101234.56
    assert data["account"]["cash"] == 50000.0


def test_invalid_snapshot_is_rejected_not_zero_filled():
    bad = account_snapshot()
    bad["cash"] = "not-a-number"
    try:
        validate_account_snapshot(bad)
    except ValueError as exc:
        assert "cash" in str(exc)
    else:
        raise AssertionError("invalid snapshot must be rejected")


def test_live_feed_retries_after_transient_failure():
    class Gateway:
        def __init__(self):
            self.calls = 0

        def snapshot(self, include_history=False):
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError("jitter")
            return account_snapshot()

    gateway = Gateway()
    feed = LiveAccountFeed(factory=lambda: gateway, interval=0.1, stale_after=1)
    feed.start()
    try:
        for _ in range(250):
            snapshot = feed.get()
            if snapshot["account"] and not snapshot["error"]:
                break
            import time
            time.sleep(0.02)
        assert snapshot["account"]["equity"] == 101234.56
        assert snapshot["error"] is None
    finally:
        feed.stop()
