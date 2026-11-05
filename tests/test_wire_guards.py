"""Exercise actual alpaca-py serialization and LumiBot contracts, without sockets."""
from datetime import datetime, timezone
from types import SimpleNamespace
import json
import pytest

from trading_bot.broker import build_broker, PaperGateway
from trading_bot.execution import execute
from test_safety_repair import INTENT, NOW


def gateway(store, monkeypatch, confirmed=True):
    import trading_bot.broker as module
    class Clock(datetime):
        current = NOW
        @classmethod
        def now(cls, tz):
            return cls.current
    monkeypatch.setattr(module, "datetime", Clock)
    b = build_broker({"ALPACA_API_KEY": "fake", "ALPACA_API_SECRET": "fake"}, start_orders_thread=False)
    g = PaperGateway(SimpleNamespace(broker=b), store, confirmed, ["TEST"])
    monkeypatch.setattr(b.api, "get_clock", lambda: SimpleNamespace(is_open=True,timestamp=Clock.current))
    monkeypatch.setattr(b.api, "get_asset", lambda symbol: SimpleNamespace(tradable=True, status=SimpleNamespace(value="active"), asset_class=SimpleNamespace(value="us_equity")))
    calls, orders = [], {}
    def transport(method, url, options, retry):
        assert url.startswith("https://paper-api.alpaca.markets/v2/")
        assert options["allow_redirects"] is False
        assert "verify" not in options or options["verify"] is True
        if method.upper() == "POST":
            wire = options["json"]
            calls.append(dict(wire))
            result = {"id": "00000000-0000-0000-0000-000000000001",
                      "client_order_id": wire["client_order_id"], "created_at": NOW.isoformat(),
                      "updated_at": NOW.isoformat(), "submitted_at": NOW.isoformat(),
                      "order_class": "simple", "time_in_force": "ioc", "status": "filled",
                      "extended_hours": False, "symbol": wire["symbol"], "side": wire["side"],
                      "qty": wire["qty"], "filled_qty": wire["qty"], "filled_avg_price": "100", "type": "limit"}
            orders[wire["client_order_id"]] = result
            return result
        if "by_client_order_id" in url:
            return orders[options["params"]["client_order_id"]]
        if method.upper() == "DELETE":
            calls.append({"delete": url})
            return None
        raise AssertionError("Unexpected fake transport request")
    monkeypatch.setattr(b.api, "_one_request", transport)
    return g, b, calls, Clock


def test_actual_sdk_wire_integer_ioc_and_durable_dispatch(store, monkeypatch):
    g, b, calls, clock = gateway(store, monkeypatch)
    cid = execute(store, g, "cycle", "2026-11-02", INTENT)
    assert len(calls) == 1
    assert calls[0]["qty"] == "10"
    assert calls[0]["time_in_force"] == "ioc" and "notional" not in calls[0]
    assert calls[0]["client_order_id"] == cid and not store.pending()
    assert store.db.execute("SELECT count(*) FROM order_dispatches").fetchone()[0] == 1
    with pytest.raises(ValueError, match="already dispatched"):
        g.submit(INTENT, cid)
    assert len(calls) == 1


@pytest.mark.parametrize("time,allowed", [("2026-11-02T14:59:30+00:00", True),
                                          ("2026-11-02T14:59:59+00:00", True),
                                          ("2026-11-02T15:00:00+00:00", False),
                                          ("2026-11-02T15:01:00+00:00", False)])
def test_final_http_deadline_guard(store, monkeypatch, time, allowed):
    g, b, calls, clock = gateway(store, monkeypatch)
    clock.current = datetime.fromisoformat(time)
    store.reserve("cid", "2026-11-02", INTENT, "cycle")
    if allowed:
        g.submit(INTENT, "cid")
        assert len(calls) == 1
    else:
        with pytest.raises(ValueError, match="window"):
            g.submit(INTENT, "cid")
        assert not calls


def test_deadline_crossed_during_gateway_checks(store, monkeypatch):
    g, b, calls, clock = gateway(store, monkeypatch)
    clock.current = datetime(2026, 11, 2, 14, 59, 59, tzinfo=timezone.utc)
    original = b.api.get_asset
    def slow_asset(symbol):
        clock.current = datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc)
        return original(symbol)
    monkeypatch.setattr(b.api, "get_asset", slow_asset)
    store.reserve("cid", "2026-11-02", INTENT)
    with pytest.raises(RuntimeError, match="outside opening"):
        g.submit(INTENT, "cid")
    assert calls == []


def test_http_cancel_after_deadline_allowed(store, monkeypatch):
    g, b, calls, clock = gateway(store, monkeypatch)
    clock.current = datetime(2026, 11, 2, 19, 0, tzinfo=timezone.utc)
    b.api.cancel_order_by_id("00000000-0000-0000-0000-000000000001")
    assert "delete" in calls[0]


@pytest.mark.parametrize("payload", [{"qty": "1.2"}, {"notional": 100}, {"time_in_force": "day"}, {"type": "market"}])
def test_lowest_http_entry_rejects_bad_orders(store, monkeypatch, payload):
    g, b, calls, clock = gateway(store, monkeypatch)
    wire = {**INTENT, "qty": 10, "client_order_id": "cid", **payload}
    with pytest.raises(ValueError):
        b.api.post("/orders", wire)
    assert not calls


def test_lowest_http_entry_cannot_skip_risk_gateway(store, monkeypatch):
    g, b, calls, clock = gateway(store, monkeypatch)
    with pytest.raises(RuntimeError, match="bypass"):
        b.api.post("/orders", {**INTENT, "qty": 10, "client_order_id": "cid"})
    assert not calls


@pytest.mark.parametrize("method,path", [("PATCH", "/orders/id"), ("DELETE", "/positions/TEST"),
                                        ("DELETE", "/orders"), ("POST", "/orders/bulk")])
def test_replacement_and_liquidation_endpoints_forbidden(store, monkeypatch, method, path):
    g, b, calls, clock = gateway(store, monkeypatch)
    with pytest.raises(RuntimeError):
        b.api._request(method, path)
    assert not calls


def test_default_ioc_unknown_prevents_post(store, monkeypatch):
    g, b, calls, clock = gateway(store, monkeypatch, False)
    assert store.safety_reason() and store.paused()
    with pytest.raises(ValueError):
        execute(store, g, "cycle", "2026-11-02", INTENT)
    assert not calls


@pytest.mark.parametrize("change", [{"quantity": 1.1}, {"notional": 10}, {"symbol": "OUTSIDE"}])
def test_gateway_entry_cannot_bypass_shape_or_pool(store, monkeypatch, change):
    g, b, calls, clock = gateway(store, monkeypatch)
    with pytest.raises(ValueError):
        g.submit({**INTENT, **change}, "cid")
    assert not calls


def test_gateway_rechecks_available_sell_shares(store, monkeypatch):
    g, b, calls, clock = gateway(store, monkeypatch)
    from trading_bot.dry_run import FakeBroker
    account = FakeBroker().snapshot()
    account.update(quantities={"TEST": 2.9}, positions={"TEST": 290})
    monkeypatch.setattr(g, "snapshot", lambda: account)
    intent = {**INTENT, "side": "sell", "quantity": 3}
    store.reserve("cid", "2026-11-02", intent)
    with pytest.raises(ValueError, match="whole shares insufficient"):
        g.submit(intent, "cid")
    assert not calls


def test_paused_between_gateway_and_wire_blocks_post(store, monkeypatch):
    g, b, calls, clock = gateway(store, monkeypatch)
    asset = b.api.get_asset
    def pause(symbol):
        store.pause(True)
        return asset(symbol)
    monkeypatch.setattr(b.api, "get_asset", pause)
    store.reserve("cid", "day", INTENT)
    with pytest.raises(ValueError, match="revoked"):
        g.submit(INTENT, "cid")
    assert not calls


def test_framework_cache_does_not_cache_execution_snapshots(monkeypatch):
    import trading_bot.broker as module
    b = build_broker({"ALPACA_API_KEY": "fake", "ALPACA_API_SECRET": "fake"}, start_orders_thread=False)
    calls = []
    monkeypatch.setattr(b.api, "_one_request", lambda *args: calls.append(args) or {"n": len(calls)})
    assert b.api.get("/account") == {"n": 1}
    assert b.api.get("/account") == {"n": 1}
    with b.api.fresh_reads():
        assert b.api.get("/account") == {"n": 2}
    assert b.api.get("/account") == {"n": 2}
    b.api.get("/orders:by_client_order_id", {"client_order_id": "cid"})
    b.api.get("/orders:by_client_order_id", {"client_order_id": "cid"})
    assert len(calls) == 4


def test_strategy_layer_requests_native_five_minutes(monkeypatch):
    import logging
    from lumibot.strategies import Strategy
    from lumibot.entities import Asset
    from alpaca.data.models import BarSet
    from alpaca.data.historical import StockHistoricalDataClient
    from trading_bot.dry_run import bars
    b = build_broker({"ALPACA_API_KEY": "fake", "ALPACA_API_SECRET": "fake"}, start_orders_thread=False)
    seen = []
    def fetch(self, request):
        seen.append(request)
        return BarSet({"TEST": [{"t": t.isoformat(), "o": 100, "h": 101, "l": 99, "c": 100, "v": 10, "n": 1, "vw": 100} for t in bars().index]})
    monkeypatch.setattr(StockHistoricalDataClient, "get_stock_bars", fetch)
    s = SimpleNamespace(broker=b, quote_asset=Asset("USD", asset_type="forex"), is_backtesting=False,
                        _logged_get_historical_prices_assets=set(), logger=logging.getLogger("fixture"),
                        _sanitize_user_asset=lambda a: a, crypto_assets_to_tuple=lambda a, quote: a,
                        get_datetime=lambda: NOW)
    s._parse_timestep = lambda timestep: Strategy._parse_timestep(s, timestep)
    result = Strategy.get_historical_prices(s, Asset("TEST"), 2, timestep="5 minutes", include_after_hours=False)
    assert seen[0].timeframe.amount == 5
    assert len(result.df) == 2


def test_sdk_post_timeout_no_transport_retry(store, monkeypatch):
    import requests
    g, b, calls, clock = gateway(store, monkeypatch)
    transport = b.api._one_request
    attempts = []
    def lost_response(method, url, options, retry):
        result = transport(method, url, options, retry)
        if method.upper() == "POST":
            attempts.append(method)
            raise requests.Timeout("POST response lost after accept")
        return result
    monkeypatch.setattr(b.api, "_one_request", lost_response)
    execute(store, g, "cycle", "2026-11-02", INTENT)
    assert len(attempts) == len(calls) == 1 and not store.pending()


def test_sdk_rejection_latches_no_day_fallback(store, monkeypatch):
    from alpaca.common.exceptions import APIError
    import requests
    g, b, calls, clock = gateway(store, monkeypatch)
    attempts = []
    response = requests.Response()
    response.status_code = 422
    def reject(method, url, options, retry):
        assert options["json"]["time_in_force"] == "ioc"
        attempts.append(method)
        raise APIError('{"code":42210000,"message":"time_in_force IOC unsupported"}',
                       requests.HTTPError(response=response))
    monkeypatch.setattr(b.api, "_one_request", reject)
    execute(store, g, "cycle", "2026-11-02", INTENT)
    assert attempts == ["POST"] and store.paused() and store.safety_reason()
    assert not store.pending()


def test_actual_adapter_timeout_and_tls(monkeypatch):
    from requests.adapters import HTTPAdapter
    b = build_broker({"ALPACA_API_KEY": "fake", "ALPACA_API_SECRET": "fake"}, start_orders_thread=False)
    seen = []
    monkeypatch.setattr(HTTPAdapter, "send", lambda self, request, **kwargs: seen.append(kwargs) or None)
    adapter = b.api._session.adapters["https://"]
    adapter.send(SimpleNamespace(method="GET", url="https://paper-api.alpaca.markets/v2/account"), verify=True)
    adapter.send(SimpleNamespace(method="DELETE", url="https://paper-api.alpaca.markets/v2/orders/id"), verify=True)
    assert seen[0]["timeout"] == (5, 20) and seen[1]["timeout"] == (3, 5)
    assert all(x["verify"] is True for x in seen)


def test_broker_clock_skew_blocks_order(store, monkeypatch):
    from datetime import timedelta
    g, b, calls, clock = gateway(store, monkeypatch)
    monkeypatch.setattr(b.api, "get_clock", lambda: SimpleNamespace(is_open=True,timestamp=NOW-timedelta(seconds=20)))
    store.reserve("cid", "day", INTENT)
    with pytest.raises(ValueError, match="clock skew"):
        g.submit(INTENT, "cid")
    assert not calls


def test_approval_expires_during_asset_lookup(store, monkeypatch):
    from datetime import timedelta
    g, b, calls, clock = gateway(store, monkeypatch)
    intent = {**INTENT, "valid_until": (NOW+timedelta(seconds=5)).isoformat()}
    store.reserve("cid", "day", intent)
    original = b.api.get_asset
    def slow_asset(symbol):
        clock.current = NOW+timedelta(seconds=10)
        return original(symbol)
    monkeypatch.setattr(b.api, "get_asset", slow_asset)
    with pytest.raises(ValueError, match="approval/quote expired"):
        g.submit(intent, "cid")
    assert not calls
