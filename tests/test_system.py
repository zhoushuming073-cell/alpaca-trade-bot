import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from trading_bot.dry_run import run as dry_run
from trading_bot.market_data import validate_bars, window
from trading_bot.offline_validation import run_validation
from trading_bot.storage import Store


def test_dry_run_uses_ai_qty_and_day_limit():
    with tempfile.TemporaryDirectory() as directory:
        store = Store(Path(directory) / "dry.sqlite")
        result = dry_run(store)
        assert result["active"] == "ORDER_RESULT"
        assert result["submissions"] == 1
        assert result["positions"]
        row = store.order_row(
            list(store.db.execute("SELECT cid FROM orders"))[0][0]
        )
        assert row["state"] == "FILLED"
        intent = json.loads(row["intent"])
        assert intent["time_in_force"] == "day"
        assert intent["qty"] == 3
        store.close()


def test_offline_end_to_end():
    result = run_validation()
    assert result["simulated_posts"] == 1
    assert result["real_or_paper_orders_submitted"] == 0
    assert result["auto_cancel_calls"] == 0
    assert result["stop_status"] == "NONE_BY_AI"
    assert result["final_position_qty"] == 3


@pytest.mark.parametrize("time,expected", [
    ("2026-11-07T15:00Z", None), ("2026-11-26T15:00Z", None),
    ("2026-11-27T18:00Z", None),
    ("2026-11-27T17:45:10Z", "2026-11-27T17:45:00+00:00"),
    ("2026-07-06T13:45:10Z", "2026-07-06T13:45:00+00:00"),
    ("2026-11-02T14:34Z", None),
])
def test_calendar(time, expected):
    value = window(pd.Timestamp(time))
    assert (value.isoformat() if value is not None else None) == expected


def test_incomplete_bar_removed(risk):
    frame = pd.DataFrame(
        {"open": [100.0, 100.0], "high": [101.0, 101.0], "low": [99.0, 99.0],
         "close": [100.0, 100.0], "volume": [10, 10]},
        index=pd.to_datetime(["2026-10-30T19:55Z", "2026-11-02T14:30Z"]),
    )
    frame.loc[pd.Timestamp("2026-11-02T14:35Z")] = [100, 101, 99, 100, 1]
    assert len(validate_bars(
        frame, pd.Timestamp("2026-11-02T14:35Z"),
        pd.Timestamp("2026-11-02T14:35:10Z"), risk, 2,
    )) == 2


def test_lumibot_contract():
    import inspect

    from lumibot.data_sources import AlpacaData
    from lumibot.strategies import Strategy

    from trading_bot.strategy import PaperStrategy

    data = AlpacaData({"API_KEY": "fake", "API_SECRET": "fake", "PAPER": True}, delay=0)
    timeframe = data._parse_source_timestep("5 minutes", reverse=True)
    assert timeframe.amount == 5
    assert issubclass(PaperStrategy, Strategy)
    assert "custom_params" in inspect.signature(Strategy.create_order).parameters


def test_paper_config_rejects_live(monkeypatch):
    from trading_bot.settings import credentials

    monkeypatch.setenv("ALPACA_IS_PAPER", "false")
    with pytest.raises(ValueError):
        credentials()


def test_config_hashes_and_snapshot(bundle):
    assert len(bundle.symbols) == 48
    assert bundle.hash == bundle.hashes["combined"]
    assert bundle.execution.time_in_force == "day"
    assert bundle.policy.ready() is True
    snapshot = __import__("trading_bot.settings", fromlist=["config_snapshot"]).config_snapshot(bundle)
    assert snapshot["hashes"] == bundle.hashes
