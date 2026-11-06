from datetime import datetime, timezone

import pandas as pd
import pytest

from trading_bot.market_data import CALENDAR, SessionPhase, daily_history, session_info


def history():
    dates = CALENDAR.schedule("2026-04-01", "2026-11-03").index.tz_localize("America/New_York")
    return pd.DataFrame(
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 10},
        index=dates,
    )


def test_six_calendar_months():
    result = daily_history(history(), pd.Timestamp("2026-11-04T15:00Z"))
    assert result.index[0].date().isoformat() == "2026-05-04"
    assert len(result) > 120
    assert result.index[-1].date().isoformat() == "2026-11-03"


def test_short_history_rejected():
    with pytest.raises(ValueError):
        daily_history(history().tail(30), pd.Timestamp("2026-11-04T15:00Z"))


def test_daily_internal_gap_rejected():
    frame = history()
    frame = frame.drop(frame.index[40])
    with pytest.raises(ValueError, match="missing_count=1"):
        daily_history(frame, pd.Timestamp("2026-11-04T15:00Z"))


def test_active_window_ends_at_noon(bundle):
    info = session_info(datetime(2026, 11, 5, 14, 30, tzinfo=timezone.utc), bundle.execution)
    assert info.phase == SessionPhase.ACTIVE
    assert info.market_open.isoformat() == "2026-11-05T14:30:00+00:00"
    assert info.active_end.astimezone(timezone.utc).isoformat() == "2026-11-05T17:00:00+00:00"
    assert session_info(
        datetime(2026, 11, 5, 17, 0, tzinfo=timezone.utc), bundle.execution
    ).phase == SessionPhase.CLOSED
