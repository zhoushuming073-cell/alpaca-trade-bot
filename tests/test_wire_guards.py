from types import SimpleNamespace

import pytest

from trading_bot.broker import PaperGateway, build_broker
from trading_bot.market_data import session_info
from trading_bot.order_state import OrderState
from trading_bot.safety import PreDispatchRejected
from trading_bot.settings import load_config
from trading_bot.storage import Store

NOW = "2026-11-05T14:35:10+00:00"


def intent(**overrides):
    value = {
        "symbol": "TEST", "side": "buy", "qty": 3, "limit_price": 100.05,
        "order_type": "limit", "time_in_force": "day", "extended_hours": False,
        "stop_policy": "NONE", "stop_loss": None,
    }
    value.update(overrides)
    return value


def prepared_gateway(tmp_path, monkeypatch):
    bundle = load_config()
    broker = build_broker({"ALPACA_API_KEY": "fake", "ALPACA_API_SECRET": "fake"},
                          start_orders_thread=False)
    store = Store(tmp_path / "wire.sqlite", instance_id="wire")
    gateway = PaperGateway(
        SimpleNamespace(broker=broker), store, ["TEST"], bundle.execution
    )
    account = {
        "account_id_hash": "x", "equity": 100000.0, "last_equity": 100000.0,
        "cash": 100000.0, "buying_power": 400000.0, "non_margin_cash": 100000.0,
        "blocked": False, "positions": {}, "position_details": {},
        "quantities": {}, "available_quantities": {}, "open_orders": [],
        "asof": "2026-11-05T14:35:00+00:00",
    }
    monkeypatch.setattr(gateway, "snapshot", lambda: account)
    monkeypatch.setattr(broker.api, "get_clock", lambda: SimpleNamespace(
        is_open=True, timestamp=__import__("datetime").datetime.fromisoformat(NOW)
    ))
    monkeypatch.setattr(broker.api, "get_asset", lambda symbol: SimpleNamespace(
        tradable=True, status=SimpleNamespace(value="active"),
        asset_class=SimpleNamespace(value="us_equity"),
    ))
    calls = []
    orders = {}

    def transport(method, url, options, retry):
        assert url.startswith("https://paper-api.alpaca.markets/v2/")
        if method.upper() == "POST":
            wire = options["json"]
            calls.append(dict(wire))
            result = {
                "id": "00000000-0000-0000-0000-000000000001",
                "client_order_id": wire["client_order_id"],
                "created_at": NOW, "updated_at": NOW, "submitted_at": NOW,
                "order_class": wire.get("order_class") or "simple",
                "time_in_force": "day", "status": "filled",
                "extended_hours": False, "symbol": wire["symbol"],
                "side": wire["side"], "qty": wire["qty"], "filled_qty": wire["qty"],
                "filled_avg_price": "100", "type": "limit",
            }
            orders[wire["client_order_id"]] = result
            return result
        raise AssertionError("unexpected transport call")
    monkeypatch.setattr(broker.api, "_one_request", transport)
    return gateway, broker, store, calls, orders


def dispatch_ready(store, bundle, cid="cid-1", value=None):
    value = value or intent()
    store.reserve_order(cid, "2026-11-05", value, owner="owner")
    assert store.claim_order(cid, "owner")
    assert store.transition(cid, OrderState.READY_TO_DISPATCH,
                            expected=OrderState.VALIDATING, owner="owner")
    assert store.record_dispatch_intent(cid, "owner")


def test_short_prepare_checks_borrow_and_dispatches_position_intent(tmp_path, monkeypatch):
    gateway, broker, store, calls, _ = prepared_gateway(tmp_path, monkeypatch)
    bundle = load_config()
    gateway.execution = bundle.execution.model_copy(update={"allow_shorting": True})
    account = {
        "account_id_hash": "x", "equity": 100000.0, "last_equity": 100000.0,
        "cash": 100000.0, "buying_power": 100000.0, "blocked": False,
        "shorting_enabled": True, "positions": {}, "position_details": {},
        "quantities": {}, "available_quantities": {}, "open_orders": [],
        "asof": NOW,
    }
    monkeypatch.setattr(gateway, "snapshot", lambda: account)
    monkeypatch.setattr(broker.api, "get_asset", lambda symbol: SimpleNamespace(
        tradable=True, status=SimpleNamespace(value="active"),
        asset_class=SimpleNamespace(value="us_equity"),
        shortable=True, easy_to_borrow=True, marginable=True,
    ))
    short = {
        "action": "OPEN_SHORT", "side": "sell", "position_intent": "sell_to_open",
        "symbol": "TEST", "qty": 2, "limit_price": 100.0,
        "order_type": "limit", "time_in_force": "day", "extended_hours": False,
        "stop_policy": "NONE", "stop_loss": None,
    }
    cid = "short-1"
    store.reserve_order(cid, "2026-11-05", short, owner="owner")
    assert store.claim_order(cid, "owner")
    assert store.transition(cid, OrderState.READY_TO_DISPATCH,
                            expected=OrderState.VALIDATING, owner="owner")
    assert store.record_dispatch_intent(cid, "owner")
    result = gateway.dispatch(short, cid)
    assert result["status"] == "filled"
    assert calls[0]["position_intent"] == "sell_to_open"
    assert calls[0]["side"] == "sell"


def test_short_prepare_rejects_asset_without_borrow(tmp_path, monkeypatch):
    gateway, broker, store, calls, _ = prepared_gateway(tmp_path, monkeypatch)
    bundle = load_config()
    gateway.execution = bundle.execution.model_copy(update={"allow_shorting": True})
    account = {
        "account_id_hash": "x", "equity": 100000.0, "last_equity": 100000.0,
        "cash": 100000.0, "buying_power": 100000.0, "blocked": False,
        "shorting_enabled": True, "positions": {}, "position_details": {},
        "quantities": {}, "available_quantities": {}, "open_orders": [],
        "asof": NOW,
    }
    monkeypatch.setattr(gateway, "snapshot", lambda: account)
    monkeypatch.setattr(broker.api, "get_clock", lambda: SimpleNamespace(
        is_open=True, timestamp=__import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        )
    ))
    monkeypatch.setattr(broker.api, "get_asset", lambda symbol: SimpleNamespace(
        tradable=True, status=SimpleNamespace(value="active"),
        asset_class=SimpleNamespace(value="us_equity"),
        shortable=False, easy_to_borrow=False, marginable=True,
    ))
    short = {
        "action": "OPEN_SHORT", "side": "sell", "position_intent": "sell_to_open",
        "symbol": "TEST", "qty": 2, "limit_price": 100.0,
        "order_type": "limit", "time_in_force": "day", "extended_hours": False,
        "stop_policy": "NONE", "stop_loss": None,
    }
    cid = "short-2"
    store.reserve_order(cid, "2026-11-05", short, owner="owner")
    assert store.claim_order(cid, "owner")
    active = session_info(__import__("datetime").datetime.fromisoformat(NOW), bundle.execution)
    with pytest.raises(PreDispatchRejected):
        gateway.prepare(short, cid, session=active)
    assert calls == []


def test_dispatch_builds_whole_share_day_limit(tmp_path, monkeypatch):
    gateway, broker, store, calls, orders = prepared_gateway(tmp_path, monkeypatch)
    bundle = load_config()
    cid = "cid-1"
    dispatch_ready(store, bundle, cid)
    session = session_info(
        __import__("datetime").datetime.fromisoformat(NOW), bundle.execution
    )
    result = gateway.dispatch(intent(), cid)
    assert result["status"] == "filled"
    assert len(calls) == 1
    wire = calls[0]
    assert wire["qty"] == "3"
    assert wire["time_in_force"] == "day"
    assert wire["type"] == "limit"
    assert "notional" not in wire
    assert wire["client_order_id"] == cid
    assert store.order_row(cid)["state"] == OrderState.DISPATCH_INTENT_RECORDED.value


def test_dispatch_rejects_wire_mismatch(tmp_path, monkeypatch):
    gateway, _, store, calls, _ = prepared_gateway(tmp_path, monkeypatch)
    cid = "cid-2"
    dispatch_ready(store, load_config(), cid)
    with pytest.raises(RuntimeError):
        gateway.dispatch(intent(qty=4), cid)
    assert calls == []


def test_prepare_rejects_outside_window_and_unverified_stop(tmp_path, monkeypatch):
    gateway, _, store, _, _ = prepared_gateway(tmp_path, monkeypatch)
    bundle = load_config()
    cid = "cid-3"
    store.reserve_order(cid, "2026-11-05", intent(), owner="owner")
    assert store.claim_order(cid, "owner")
    closed = session_info(
        __import__("datetime").datetime.fromisoformat("2026-11-05T17:05:00+00:00"),
        bundle.execution,
    )
    with pytest.raises(PreDispatchRejected):
        gateway.prepare(intent(), cid, session=closed)
    active = session_info(
        __import__("datetime").datetime.fromisoformat(NOW), bundle.execution
    )
    with pytest.raises(Exception):
        gateway.prepare(
            intent(stop_policy="BROKER_STOP", stop_loss={"stop_price": 95}),
            cid, session=active,
        )


def test_snapshot_does_not_treat_buying_power_as_cash(tmp_path, monkeypatch):
    bundle = load_config()
    broker = build_broker({"ALPACA_API_KEY": "fake", "ALPACA_API_SECRET": "fake"},
                          start_orders_thread=False)
    store = Store(tmp_path / "snap.sqlite", instance_id="snap")
    gateway = PaperGateway(SimpleNamespace(broker=broker), store, ["TEST"], bundle.execution)
    monkeypatch.setattr(broker.api, "get_account", lambda: SimpleNamespace(
        account_number="acct", equity="100000", last_equity="100000", cash="25000",
        buying_power="100000", trading_blocked=False, account_blocked=False,
    ))
    monkeypatch.setattr(broker.api, "get_all_positions", lambda: [
        SimpleNamespace(symbol="TEST", qty="10", qty_available="8", market_value="1000",
                        avg_entry_price="100", unrealized_pl="0", current_price="100"),
    ])
    monkeypatch.setattr(broker.api, "get_orders", lambda **kwargs: [])
    account = gateway.snapshot()
    assert account["cash"] == 25000
    assert account["non_margin_cash"] == 25000
    assert account["available_quantities"]["TEST"] == 8
    assert account["position_details"]["TEST"]["qty"] == 10


def test_post_timeout_is_not_retried(tmp_path, monkeypatch):
    gateway, broker, store, calls, _ = prepared_gateway(tmp_path, monkeypatch)
    cid = "cid-4"
    dispatch_ready(store, load_config(), cid)

    def lost(method, url, options, retry):
        calls.append(dict(options.get("json") or {}))
        raise TimeoutError("response lost")

    monkeypatch.setattr(broker.api, "_one_request", lost)
    with pytest.raises(TimeoutError):
        gateway.dispatch(intent(), cid)
    assert len(calls) == 1


def test_verified_broker_stop_uses_oto_and_stop_leg(tmp_path, monkeypatch):
    gateway, broker, store, calls, _ = prepared_gateway(tmp_path, monkeypatch)
    bundle = load_config()
    gateway.execution = bundle.execution.model_copy(
        update={"allow_broker_stop": True, "broker_stop_verified": True}
    )
    cid = "cid-stop"
    value = intent(stop_policy="BROKER_STOP", stop_loss={"stop_price": 95})
    dispatch_ready(store, bundle, cid, value)
    result = gateway.dispatch(value, cid)
    assert result["status"] == "filled"
    wire = calls[0]
    assert wire["order_class"] == "oto"
    assert wire["stop_loss"]["stop_price"] == 95
    assert "take_profit" not in wire


def test_unverified_stop_never_reaches_transport(tmp_path, monkeypatch):
    gateway, broker, store, calls, _ = prepared_gateway(tmp_path, monkeypatch)
    cid = "cid-stop-2"
    value = intent(stop_policy="BROKER_STOP", stop_loss={"stop_price": 95})
    dispatch_ready(store, load_config(), cid, value)
    with pytest.raises(Exception):
        gateway.dispatch(value, cid)
    assert calls == []
