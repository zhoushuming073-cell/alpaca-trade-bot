from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from trading_bot.broker import PaperGateway
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
    assert "brief" in data and "plan_history" in data


def test_research_detail_endpoint_reads_real_profile(tmp_path, monkeypatch):
    store = Store(tmp_path / "web.sqlite")
    store.save_research_profile({
        "symbol": "CRWV", "status": "OK", "source_status": "AVAILABLE",
        "verified_at_utc": "2026-11-05T00:00:00+00:00",
        "facts": ["real fact"],
    }, evidence=[{
        "kind": "news", "source_url": "https://example.invalid/article",
        "title": "real source", "published_at": "2026-11-04T00:00:00+00:00",
        "retrieved_at": "2026-11-05T00:00:00+00:00", "verified": 1,
        "content_hash": "hash", "summary": "real summary",
    }], revision=None)
    store.close()
    c = client(tmp_path, monkeypatch, FakeFeed(account_snapshot()))
    response = c.get("/api/research/CRWV", base_url=ORIGIN)
    assert response.status_code == 200
    data = response.get_json()
    assert data["profile"]["facts"] == ["real fact"]
    assert data["evidence"][0]["title"] == "real source"
    assert data["symbol"] == "CRWV"


def test_robot_events_are_real_and_incremental(tmp_path, monkeypatch):
    store = Store(tmp_path / "web.sqlite")
    store.event("premarket_plan", {
        "trading_day": "2026-11-05", "market_decision": "WATCH", "candidates": 7,
    })
    store.event("order", {
        "symbol": "AAPL", "side": "buy", "qty": "1", "filled_qty": "1",
        "status": "filled", "cid": "aip-real", "filled_avg_price": "353.63",
    })
    store.event("safety_alert", {"reason": "TEST_FREEZE", "outcome": "new_orders_disabled"})
    store.close()
    c = client(tmp_path, monkeypatch, FakeFeed(account_snapshot()))
    response = c.get("/api/robot-events", base_url=ORIGIN)
    assert response.status_code == 200
    data = response.get_json()
    by_kind = {item["kind"]: item for item in data["events"]}
    assert by_kind["premarket_plan"]["category"] == "ai"
    assert by_kind["premarket_plan"]["source"] == "AI"
    assert by_kind["order"]["category"] == "fill"
    assert by_kind["order"]["source"] == "Alpaca"
    assert by_kind["safety_alert"]["severity"] == "critical"
    assert data["next_after"] == max(item["id"] for item in data["events"])
    incremental = c.get("/api/robot-events?after_id=" + str(data["next_after"]), base_url=ORIGIN).get_json()
    assert incremental["events"] == []


def test_optional_closed_order_failure_does_not_break_account_snapshot(bundle):
    class Api:
        @contextmanager
        def fresh_reads(self):
            yield

        def get_account(self):
            return SimpleNamespace(
                equity=1000.0, last_equity=990.0, cash=500.0, buying_power=1500.0,
                portfolio_value=1000.0, trading_blocked=False, account_blocked=False,
                currency="USD", status="ACTIVE", daytrade_count=0,
                long_market_value=500.0, short_market_value=0.0,
                initial_margin=0.0, maintenance_margin=0.0,
            )

        def get_all_positions(self):
            return []

        def get_orders(self, filter):
            if str(getattr(filter.status, "value", filter.status)).lower() == "open":
                return []
            raise TimeoutError("closed-order endpoint jitter")

    gateway = PaperGateway(SimpleNamespace(broker=SimpleNamespace(api=Api())), None, bundle.symbols, bundle.execution)
    snapshot = gateway.snapshot(include_history=True)
    assert snapshot["equity"] == 1000.0
    assert snapshot["recent_orders"] == []
    assert "TimeoutError" in snapshot["recent_orders_error"]


def test_broker_filled_state_overrides_stale_local_accepted(tmp_path, monkeypatch):
    store = Store(tmp_path / "web.sqlite")
    cid = "old-real-order"
    store.reserve_order(cid, "2026-11-05", {
        "symbol": "IREN", "side": "buy", "qty": 100, "limit_price": 22.0,
    }, owner="owner")
    assert store.claim_order(cid, "owner")
    assert store.transition(cid, OrderState.READY_TO_DISPATCH,
                            expected=OrderState.VALIDATING, owner="owner")
    assert store.record_dispatch_intent(cid, "owner")
    store.apply_broker_order(cid, {
        "id": "broker-old", "client_order_id": cid, "symbol": "IREN",
        "side": "buy", "qty": "100", "filled_qty": "0", "status": "new",
        "type": "limit",
    })
    store.close()
    c = client(tmp_path, monkeypatch, FakeFeed(account_snapshot()))
    data = c.get("/api/status", base_url=ORIGIN).get_json()
    row = [item for item in data["orders"] if item.get("cid") == cid][0]
    assert row["status"] == "filled"
    assert row["state"] == "FILLED"
    assert row["local_state"] == OrderState.ACCEPTED.value
    assert row["state_mismatch"] is True


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
