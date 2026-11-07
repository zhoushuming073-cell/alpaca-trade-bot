from datetime import datetime, timezone

import pytest

from trading_bot.dashboard import ORIGIN, create_app
from trading_bot.order_state import OrderState
from trading_bot.storage import Store


def client(tmp_path, monkeypatch):
    import trading_bot.dashboard as dashboard

    monkeypatch.setattr(dashboard, "worker_processes", lambda: [])
    monkeypatch.setattr(dashboard, "start_worker", lambda: None)
    monkeypatch.setattr(dashboard, "terminate_workers", lambda *args, **kwargs: None)
    app = create_app(tmp_path / "web.sqlite")
    return app.test_client()


def accepted_order(store, cid="aip-day"):
    store.reserve_order(cid, "2026-11-05", {
        "symbol": "CRWV", "side": "buy", "qty": 1, "limit_price": 100.0,
    }, owner="owner")
    assert store.claim_order(cid, "owner")
    assert store.transition(cid, OrderState.READY_TO_DISPATCH,
                            expected=OrderState.VALIDATING, owner="owner")
    assert store.record_dispatch_intent(cid, "owner")
    store.apply_broker_order(cid, {
        "id": "broker-1", "client_order_id": cid, "symbol": "CRWV", "side": "buy",
        "qty": "1", "filled_qty": "0", "status": "new", "type": "limit",
        "time_in_force": "day",
    })
    return cid


def test_control_requires_token_and_origin(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    assert c.post("/api/control/resume", base_url=ORIGIN).status_code == 403
    assert c.get("/api/status", base_url="http://evil.test").status_code == 403


def test_authenticated_page_close_requires_reconciliation_and_preserves_pause(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    status = c.get("/api/status", base_url=ORIGIN).get_json()
    headers = {"Origin": ORIGIN, "X-Control-Token": status["control_token"]}
    assert c.post("/api/control/pause", base_url=ORIGIN, headers=headers).status_code == 200
    assert c.post("/api/heartbeat", base_url=ORIGIN, headers=headers).get_json()["web_connected"]
    assert c.post("/api/heartbeat/close", base_url=ORIGIN, headers={"Origin": ORIGIN}).status_code == 403
    response = c.post("/api/heartbeat/close", base_url=ORIGIN, headers=headers)
    assert response.status_code == 200
    assert response.get_json()["web_connected"] is False
    resumed_heartbeat = c.post("/api/heartbeat", base_url=ORIGIN, headers=headers).get_json()
    assert resumed_heartbeat["web_connected"] is True
    assert resumed_heartbeat["reconnect_required"] is True
    assert resumed_heartbeat["paused"] is True


def test_status_fields(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    status = c.get("/api/status", base_url=ORIGIN)
    assert status.status_code == 200
    data = status.get_json()
    assert data["service"] == "ai-paper-trader"
    assert data["running"] is False
    assert len(data["symbols"]) == 48
    assert "control_token" in data
    assert data["health"]["watchdog"]["alive"] is False
    assert data["execution"]["time_in_force"] == "day"
    assert data["phase"] in {"PREMARKET", "ACTIVE", "CLOSED", "FROZEN", "NO_SESSION"}


def test_health_does_not_fetch_accounts_or_read_snapshot(tmp_path, monkeypatch):
    import trading_bot.dashboard as dashboard
    c = client(tmp_path, monkeypatch)
    monkeypatch.setattr(dashboard, "snapshot", lambda *a, **k: pytest.fail("readiness must be local"))
    response = c.get("/api/health", base_url=ORIGIN)
    assert response.status_code == 200
    assert response.get_json() == {"service": "ai-paper-trader", "ready": True}
    assert c.get("/api/health", base_url="http://evil.test").status_code == 403


def test_pause_and_resume_allow_open_day_order(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    store = Store(tmp_path / "web.sqlite")
    accepted_order(store)
    store.close()
    status = c.get("/api/status", base_url=ORIGIN).get_json()
    headers = {"Origin": ORIGIN, "X-Control-Token": status["control_token"]}
    assert c.post("/api/control/pause", base_url=ORIGIN, headers=headers).status_code == 200
    assert c.post("/api/control/resume", base_url=ORIGIN, headers=headers).status_code == 200
    assert c.get("/api/status", base_url=ORIGIN).get_json()["paused"] is False


def test_unknown_order_blocks_resume_stop_restart(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    store = Store(tmp_path / "web.sqlite")
    store.import_broker_order("aip-unknown", {
        "id": "broker-2", "client_order_id": "aip-unknown", "symbol": "CRWV",
        "side": "buy", "qty": "1", "filled_qty": "0", "status": "new",
    })
    store.close()
    status = c.get("/api/status", base_url=ORIGIN).get_json()
    headers = {"Origin": ORIGIN, "X-Control-Token": status["control_token"]}
    assert c.post("/api/control/resume", base_url=ORIGIN, headers=headers).status_code == 409
    assert c.post("/api/control/stop", base_url=ORIGIN, headers=headers).status_code == 409
    assert c.post("/api/control/restart", base_url=ORIGIN, headers=headers).status_code == 409


def test_config_pending_is_visible(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    store = Store(tmp_path / "web.sqlite")
    store.set_active_config({"symbols": ["CRWV"], "hashes": {"combined": "stale-hash"},
                             "version": "old", "execution": {}})
    store.close()
    data = c.get("/api/status", base_url=ORIGIN).get_json()
    assert data["config_pending_restart"] is True
    assert data["active_config_hash"] == "stale-hash"
    assert data["symbols"] == ["CRWV"]


def test_stop_and_restart_are_allowed_without_unknown(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    store = Store(tmp_path / "web.sqlite")
    accepted_order(store)
    store.close()
    status = c.get("/api/status", base_url=ORIGIN).get_json()
    headers = {"Origin": ORIGIN, "X-Control-Token": status["control_token"]}
    assert c.post("/api/control/restart", base_url=ORIGIN, headers=headers).status_code == 200
    assert c.post("/api/control/stop", base_url=ORIGIN, headers=headers).status_code == 200
