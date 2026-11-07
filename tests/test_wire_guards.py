from types import SimpleNamespace
from datetime import datetime

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
    import trading_bot.broker as broker_module

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromisoformat(NOW).astimezone(tz)

    monkeypatch.setattr(broker_module, "datetime", FrozenDateTime)
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
        is_open=True, timestamp=datetime.fromisoformat(NOW)
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


def test_prepare_rejects_exactly_at_noon_cutoff(tmp_path, monkeypatch):
    from trading_bot.market_data import session_info
    gateway, _, store, _, _ = prepared_gateway(tmp_path, monkeypatch)
    bundle = load_config()
    cid = "cid-noon"
    store.reserve_order(cid, "2026-11-05", intent(), owner="owner")
    assert store.claim_order(cid, "owner")
    cutoff = session_info(
        __import__("datetime").datetime.fromisoformat("2026-11-05T16:59:30+00:00"),
        bundle.execution,
    )
    assert cutoff.can_submit is False
    with pytest.raises(PreDispatchRejected):
        gateway.prepare(intent(), cid, session=cutoff)


def test_gateway_rejects_cancel_and_post_at_exact_cutoff(tmp_path, monkeypatch):
    import trading_bot.broker as broker_module

    gateway, _, store, calls, _ = prepared_gateway(tmp_path, monkeypatch)
    cutoff_at = datetime.fromisoformat("2026-11-05T16:59:30+00:00")

    class CutoffDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cutoff_at.astimezone(tz)

    monkeypatch.setattr(broker_module, "datetime", CutoffDateTime)
    monkeypatch.setattr(gateway, "lookup", lambda cid: pytest.fail("cancel queried broker after cutoff"))
    with pytest.raises(RuntimeError, match="Cancellation is forbidden"):
        gateway.cancel("pending")
    cid = "cid-cutoff-dispatch"
    dispatch_ready(store, load_config(), cid, intent())
    with pytest.raises(PreDispatchRejected, match="cutoff"):
        gateway.dispatch(intent(), cid)
    assert calls == []


def test_cancel_lookup_crossing_cutoff_never_reaches_http_delete(tmp_path, monkeypatch):
    import trading_bot.broker as broker_module
    from trading_bot.decision_schema import OrderIntent
    from trading_bot.one_shot_engine import OneShotDecision, decision_fingerprint

    gateway, broker, store, _, _ = prepared_gateway(tmp_path, monkeypatch)
    gateway.execution = gateway.execution.model_copy(update={"write_guard_buffer_seconds": 0})
    broker.api.write_execution = gateway.execution
    now = [datetime.fromisoformat("2026-11-05T16:59:29+00:00")]

    class ClockDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now[0].astimezone(tz)

    monkeypatch.setattr(broker_module, "datetime", ClockDateTime)
    cid = "cancel-crossing-cutoff"
    dispatch_ready(store, load_config(), cid, intent())
    store.apply_broker_order(cid, {
        "id": "00000000-0000-0000-0000-000000000001", "status": "accepted",
        "client_order_id": cid, "filled_qty": "0",
    })
    decision = OneShotDecision(
        decision_id="close-after-cancel", snapshot_id="0" * 32,
        action="PLACE_ORDER", reason="Offline close test", cancel_conflicting_orders=True,
        order_intent=OrderIntent(action="SELL", symbol="TEST", qty=3, limit_price="100.05"),
    )
    assert store.reserve_one_shot_decision(decision.decision_id, decision_fingerprint(decision),
                                           decision.action) == "NEW"
    lookups = []

    def slow_lookup(value):
        assert value == cid
        lookups.append(value)
        now[0] = datetime.fromisoformat("2026-11-05T16:59:31+00:00")
        return {"id": "00000000-0000-0000-0000-000000000001", "client_order_id": cid,
                "symbol": "TEST", "side": "buy", "status": "accepted"}

    monkeypatch.setattr(gateway, "lookup", slow_lookup)
    writes = []
    monkeypatch.setattr(broker.api, "_one_request", lambda method, url, options, retry: writes.append(method))
    with pytest.raises(RuntimeError, match="WRITE_CUTOFF"):
        gateway.cancel(cid, decision=decision)
    assert lookups == [cid]
    assert writes == []


def test_post_sdk_entry_crossing_cutoff_never_reaches_http_post(tmp_path, monkeypatch):
    import trading_bot.broker as broker_module

    gateway, broker, store, _, _ = prepared_gateway(tmp_path, monkeypatch)
    gateway.execution = gateway.execution.model_copy(update={"write_guard_buffer_seconds": 0})
    broker.api.write_execution = gateway.execution
    now = [datetime.fromisoformat("2026-11-05T16:59:29+00:00")]

    class ClockDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now[0].astimezone(tz)

    monkeypatch.setattr(broker_module, "datetime", ClockDateTime)
    cid = "post-crossing-cutoff"
    dispatch_ready(store, load_config(), cid, intent())
    real_request = broker.api._request

    def late_request(method, path, data=None, **kwargs):
        now[0] = datetime.fromisoformat("2026-11-05T16:59:31+00:00")
        return real_request(method, path, data, **kwargs)

    monkeypatch.setattr(broker.api, "_request", late_request)
    writes = []
    monkeypatch.setattr(broker.api, "_one_request", lambda method, url, options, retry: writes.append(method))
    with pytest.raises(RuntimeError, match="WRITE_CUTOFF"):
        gateway.dispatch(intent(), cid)
    assert writes == []


@pytest.mark.parametrize("stamp,allowed", [
    ("2026-11-05T16:59:29.999000+00:00", True),
    ("2026-11-05T16:59:30+00:00", False),
    ("2026-11-05T16:59:30.001000+00:00", False),
    ("2026-11-26T16:59:29+00:00", False),  # NYSE holiday
    ("2026-07-06T15:59:30+00:00", False),  # DST boundary
])
def test_final_sdk_transport_gate_checks_post_and_delete(
    tmp_path, monkeypatch, stamp, allowed,
):
    from alpaca.trading.client import TradingClient
    from trading_bot.broker import WritePermit
    from trading_bot.one_shot_engine import OneShotDecision, decision_fingerprint
    from trading_bot.decision_schema import OrderIntent

    gateway, broker, store, _, _ = prepared_gateway(tmp_path, monkeypatch)
    gateway.execution = gateway.execution.model_copy(update={"write_guard_buffer_seconds": 0})
    broker.api.write_execution = gateway.execution
    now = datetime.fromisoformat(stamp)
    broker.api.write_clock = lambda: now
    monkeypatch.delattr(broker.api, "_one_request")  # Exercise PaperClient's actual final guard.
    calls = []

    def fake_transport(self, method, url, opts, retry):
        calls.append((method, url))
        return {"synthetic": True}

    monkeypatch.setattr(TradingClient, "_one_request", fake_transport, raising=False)
    post_cid = "transport-post"
    dispatch_ready(store, load_config(), post_cid, intent())
    broker.api.permits.transport = WritePermit(
        method="POST", path="/orders", cid=post_cid,
        expires_at=now.replace(year=now.year + 1), validate=lambda data: None,
        request_checked=True,
    )
    post_call = lambda: broker.api._one_request(
        "POST", "https://paper-api.alpaca.markets/v2/orders", {"json": intent()}, 0,
    )
    if allowed:
        assert post_call() == {"synthetic": True}
    else:
        with pytest.raises(RuntimeError, match="WRITE_CUTOFF"):
            post_call()
    assert len(calls) == int(allowed)

    cancel_cid = "transport-delete"
    dispatch_ready(store, load_config(), cancel_cid, intent())
    store.apply_broker_order(cancel_cid, {
        "id": "123", "status": "accepted", "client_order_id": cancel_cid, "filled_qty": "0",
    })
    decision = OneShotDecision(
        decision_id="transport-close", snapshot_id="0" * 32, action="PLACE_ORDER",
        reason="Offline DELETE contract", cancel_conflicting_orders=True,
        order_intent=OrderIntent(action="SELL", symbol="TEST", qty=3, limit_price="100.05"),
    )
    fingerprint = decision_fingerprint(decision)
    store.reserve_one_shot_decision(decision.decision_id, fingerprint, decision.action)
    broker.api.permits.transport = WritePermit(
        method="DELETE", path="/orders/123", cid=cancel_cid,
        expires_at=now.replace(year=now.year + 1), validate=lambda data: None,
        decision_id=decision.decision_id, decision_fingerprint=fingerprint,
        request_checked=True,
    )
    delete_call = lambda: broker.api._one_request(
        "DELETE", "https://paper-api.alpaca.markets/v2/orders/123", {"json": None}, 0,
    )
    if allowed:
        assert delete_call() == {"synthetic": True}
    else:
        with pytest.raises(RuntimeError, match="WRITE_CUTOFF"):
            delete_call()
    assert len(calls) == 2 * int(allowed)


@pytest.mark.parametrize("qty,price", [(3, "99.25"), (4, "99.50"), (4, "99.25")])
def test_real_gateway_engine_blocks_unapproved_same_direction_http_post(
    tmp_path, monkeypatch, qty, price,
):
    from trading_bot.decision_schema import OrderIntent
    from trading_bot.one_shot_engine import OneShotDecision, OneShotEngine

    gateway, broker, store, _, _ = prepared_gateway(tmp_path, monkeypatch)
    gateway.symbols = {"CRWV"}
    store.web_heartbeat()
    bundle = load_config()
    broker_orders = {}
    http_posts = []

    def snapshot(include_history=False):
        return {
            "account_id_hash": "offline-paper", "asof": NOW,
            "cash": 100000.0, "equity": 100000.0, "last_equity": 100000.0,
            "buying_power": 100000.0, "non_margin_cash": 100000.0,
            "blocked": False, "positions": {}, "position_details": {},
            "quantities": {}, "available_quantities": {},
            "open_orders": list(broker_orders.values()), "recent_orders": [],
        }

    def transport(method, url, options, retry):
        assert method.upper() == "POST" and url.endswith("/v2/orders")
        wire = options["json"]
        http_posts.append(dict(wire))
        order = {
            "id": f"00000000-0000-0000-0000-{len(http_posts):012d}",
            "client_order_id": wire["client_order_id"], "symbol": wire["symbol"],
            "side": str(getattr(wire["side"], "value", wire["side"])),
            "qty": wire["qty"], "filled_qty": "0",
            "limit_price": wire["limit_price"], "type": "limit", "time_in_force": "day",
            "status": "accepted", "order_class": "simple", "extended_hours": False,
            "created_at": NOW, "submitted_at": NOW, "updated_at": NOW,
        }
        broker_orders[order["client_order_id"]] = order
        return order

    monkeypatch.setattr(gateway, "snapshot", snapshot)
    monkeypatch.setattr(gateway, "lookup", lambda cid: broker_orders.get(cid))
    monkeypatch.setattr(gateway, "is_open", lambda: True)
    monkeypatch.setattr(gateway, "quote", lambda symbol, risk=None: {
        "bid": 99.99, "ask": 100.01, "last": 100.0,
        "quote_age_seconds": 0.0, "last_age_seconds": 0.0,
        "timestamp": NOW, "last_timestamp": NOW,
    })
    monkeypatch.setattr(broker.api, "_one_request", transport)
    engine = OneShotEngine(store, gateway, None, bundle,
                           clock=lambda: datetime.fromisoformat(NOW),
                           require_watchdog=False, check_disk_config=False)

    def decide(context, decision_id, shares, limit, additional=False):
        return OneShotDecision(
            decision_id=decision_id, snapshot_id=context["snapshot_id"],
            action="PLACE_ORDER", reason="Offline SDK transport test",
            intentional_additional_pending=additional,
            order_intent=OrderIntent(action="BUY", symbol="CRWV", qty=shares,
                                     limit_price=limit),
        )

    first = engine.context()
    assert engine.apply_decision(decide(first, "first", 3, "99.50"), first)["state"] == "ACCEPTED"
    second = engine.context()
    assert len(second["account"]["open_orders"]) == 1
    blocked = engine.apply_decision(decide(second, "second", qty, price), second)
    assert blocked == {"status": "BLOCKED", "reason": "ADDITIONAL_PENDING_INTENT_REQUIRES_AUTHORIZATION"}
    assert len(http_posts) == 1
    third = engine.context()
    allowed = engine.apply_decision(decide(third, "third", qty, price, True), third)
    assert allowed["state"] == "ACCEPTED"
    assert len(http_posts) == 2
    replay_context = engine.context()
    assert engine.apply_decision(decide(replay_context, "third", qty, price, True),
                                 replay_context)["status"] == "DUPLICATE"
    assert len(http_posts) == 2


def test_direct_sdk_writes_without_durable_permit_never_reach_transport(tmp_path, monkeypatch):
    from alpaca.trading.client import TradingClient

    gateway, broker, _, _, _ = prepared_gateway(tmp_path, monkeypatch)
    monkeypatch.delattr(broker.api, "_one_request")
    calls = []
    monkeypatch.setattr(TradingClient, "_one_request",
                        lambda self, method, url, opts, retry: calls.append(method), raising=False)
    broker.api.write_clock = lambda: datetime.fromisoformat(NOW)
    for method, path in (("POST", "/orders"), ("DELETE", "/orders/123")):
        with pytest.raises(RuntimeError, match="MISSING_WRITE_PERMIT"):
            broker.api._one_request(
                method, "https://paper-api.alpaca.markets/v2" + path, {"json": intent()}, 0,
            )
    with pytest.raises(RuntimeError, match="Unapproved broker write method"):
        broker.api._one_request("PATCH", "https://paper-api.alpaca.markets/v2/orders/123",
                                {"json": {}}, 0)
    assert calls == []
    with pytest.raises(RuntimeError, match="Explicit OneShot cancellation decision required"):
        gateway.cancel("not-authorized")


def test_authorized_cancel_uses_single_sdk_delete_and_rejects_wrong_side(tmp_path, monkeypatch):
    from alpaca.trading.client import TradingClient
    from trading_bot.decision_schema import OrderIntent
    from trading_bot.one_shot_engine import OneShotDecision, decision_fingerprint

    gateway, broker, store, _, _ = prepared_gateway(tmp_path, monkeypatch)
    broker.api.write_clock = lambda: datetime.fromisoformat(NOW)
    monkeypatch.delattr(broker.api, "_one_request")
    writes = []
    monkeypatch.setattr(TradingClient, "_one_request",
                        lambda self, method, url, opts, retry: writes.append((method, url)), raising=False)
    cid = "authorized-cancel"
    dispatch_ready(store, load_config(), cid, intent())
    store.apply_broker_order(cid, {
        "id": "00000000-0000-0000-0000-000000000001", "status": "accepted",
        "client_order_id": cid, "filled_qty": "0",
    })
    decision = OneShotDecision(
        decision_id="authorized-close", snapshot_id="0" * 32,
        action="PLACE_ORDER", reason="Offline close test", cancel_conflicting_orders=True,
        order_intent=OrderIntent(action="SELL", symbol="TEST", qty=3, limit_price="100.05"),
    )
    assert store.reserve_one_shot_decision(decision.decision_id, decision_fingerprint(decision),
                                           decision.action) == "NEW"
    broker_order = {
        "id": "00000000-0000-0000-0000-000000000001", "client_order_id": cid,
        "symbol": "TEST", "side": "buy", "status": "accepted",
    }
    monkeypatch.setattr(gateway, "lookup", lambda value: broker_order)
    gateway.cancel(cid, decision=decision)
    assert writes == [("DELETE", "https://paper-api.alpaca.markets/v2/orders/" + broker_order["id"])]
    broker_order["side"] = "sell"
    with pytest.raises(RuntimeError, match="does not conflict"):
        gateway.cancel(cid, decision=decision)
    assert len(writes) == 1
