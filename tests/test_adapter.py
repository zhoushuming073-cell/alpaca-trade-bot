from types import SimpleNamespace

import pandas as pd
import pytest


def bars():
    return pd.DataFrame(
        {"open": [100.0, 100.0], "high": [101.0, 101.0], "low": [99.0, 99.0],
         "close": [100.0, 100.0], "volume": [10, 10]},
        index=pd.to_datetime(["2026-10-30T19:55Z", "2026-11-02T14:30Z"]),
    )


def test_paper_endpoint_and_iex(monkeypatch):
    from alpaca.data.enums import DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    from trading_bot.broker import build_broker

    broker = build_broker({"ALPACA_API_KEY": "fake", "ALPACA_API_SECRET": "fake"}, start_orders_thread=False)
    assert str(broker.api._base_url) == "https://paper-api.alpaca.markets"
    assert broker.api._retry == 0
    broker.api._base_url = "https://api.alpaca.markets"
    with pytest.raises(RuntimeError):
        broker.api.get_account()
    seen = []
    monkeypatch.setattr(StockHistoricalDataClient, "get_stock_bars", lambda self, request: seen.append(request.feed))
    broker.data_source._get_stock_client().get_stock_bars(
        StockBarsRequest(symbol_or_symbols="TEST", timeframe=TimeFrame.Minute)
    )
    assert seen == [DataFeed.IEX]


def test_lumibot_order_client_id():
    from lumibot.entities import Asset, Order

    from trading_bot.broker import build_broker

    broker = build_broker({"ALPACA_API_KEY": "fake", "ALPACA_API_SECRET": "fake"}, start_orders_thread=False)
    seen = []

    def submit(order_data):
        seen.append(order_data)
        return SimpleNamespace(id="fake-id", status="new")

    broker.api = SimpleNamespace(submit_order=submit)
    order = Order(
        "fixture", Asset("TEST"), 1, "buy", limit_price=100, time_in_force="day",
        custom_params={"client_order_id": "fixture-cid", "extended_hours": False},
    )
    broker._submit_order(order)
    assert seen[0].client_order_id == "fixture-cid"
    assert seen[0].extended_hours is False
    assert seen[0].time_in_force == "day"


def test_regular_filter_early_close():
    from trading_bot.market_data import regular_bars

    frame = pd.DataFrame(
        {"close": [1, 1, 1, 1]},
        index=pd.to_datetime([
            "2026-11-27T14:15Z", "2026-11-27T14:30Z",
            "2026-11-27T17:55Z", "2026-11-27T18:00Z",
        ]),
    )
    assert len(regular_bars(frame)) == 2


def test_history_request_adapter(monkeypatch, risk):
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.models import BarSet
    from lumibot.entities import Asset

    from trading_bot.broker import build_broker
    from trading_bot.market_data import validate_bars

    seen = []

    def fetch(self, request):
        seen.append(request)
        return BarSet({"TEST": [
            {"t": stamp.isoformat(), "o": 100, "h": 101, "l": 99, "c": 100, "v": 10, "n": 1, "vw": 100}
            for stamp in bars().index
        ]})

    monkeypatch.setattr(StockHistoricalDataClient, "get_stock_bars", fetch)
    broker = build_broker({"ALPACA_API_KEY": "fake", "ALPACA_API_SECRET": "fake"}, start_orders_thread=False)
    result = broker.data_source.get_historical_prices(
        Asset("TEST"), 2, timestep="5 minutes", include_after_hours=False
    )
    assert seen[0].timeframe.amount == 5
    assert seen[0].feed.value == "iex"
    assert len(validate_bars(
        result.df, pd.Timestamp("2026-11-02T14:35Z"),
        pd.Timestamp("2026-11-02T14:35:10Z"), risk, 2,
    )) == 2


def test_shared_cache_wrapper_has_no_credentials(monkeypatch):
    from trading_bot.broker import build_broker

    broker = build_broker({"ALPACA_API_KEY": "fake", "ALPACA_API_SECRET": "fake"}, start_orders_thread=False)
    client = broker.data_source._get_stock_client()
    assert not hasattr(client, "_api_key")
