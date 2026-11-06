import re
from dataclasses import dataclass
from datetime import datetime, time as dtime
from enum import StrEnum
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pandas_market_calendars as mcal

CALENDAR = mcal.get_calendar("NYSE")
ET = ZoneInfo("America/New_York")


def _clock_time(value, default):
    raw = str(value or default)
    parts = raw.split(":")
    if len(parts) == 2:
        parts.append("0")
    if len(parts) != 3:
        raise ValueError("Invalid execution time")
    return dtime(int(parts[0]), int(parts[1]), int(parts[2]))


def _et(day, clock):
    return datetime.combine(day, clock, tzinfo=ET)


class SessionPhase(StrEnum):
    PREMARKET = "PREMARKET"
    ACTIVE = "ACTIVE"
    CLOSED = "CLOSED"
    NO_SESSION = "NO_SESSION"


@dataclass(frozen=True)
class SessionInfo:
    phase: SessionPhase
    trading_day: str
    now_utc: datetime
    now_et: datetime
    market_open: datetime | None
    market_close: datetime | None
    premarket_start: datetime | None
    active_end: datetime | None
    submit_cutoff: datetime | None
    can_submit: bool
    active: bool
    premarket: bool
    reason: str

    @property
    def is_trading_day(self) -> bool:
        return self.phase not in {SessionPhase.NO_SESSION}


def _utc(now) -> pd.Timestamp:
    stamp = pd.Timestamp(now)
    if stamp.tzinfo is None:
        raise ValueError("Timezone required")
    return stamp.tz_convert("UTC")


def regular_bars(df):
    if df.empty:
        return df
    if df.index.tz is None:
        raise ValueError("Missing timezone")
    schedule = CALENDAR.schedule(df.index[0].date(), df.index[-1].date())
    mask = np.zeros(len(df), dtype=bool)
    for _, row in schedule.iterrows():
        mask |= (df.index >= row.market_open) & (df.index + pd.Timedelta(minutes=5) <= row.market_close)
    return df.loc[mask]


def window(now):
    now = pd.Timestamp(now)
    if now.tzinfo is None:
        raise ValueError("Timezone required")
    date = now.tz_convert("America/New_York").date()
    schedule = CALENDAR.schedule(start_date=date, end_date=date)
    if schedule.empty:
        return None
    opening, closing = schedule.iloc[0][["market_open", "market_close"]]
    if not opening + pd.Timedelta(minutes=5) <= now < closing:
        return None
    end = opening + ((now - opening) // pd.Timedelta(minutes=5)) * pd.Timedelta(minutes=5)
    return end


def session_info(now, execution=None) -> SessionInfo:
    """Authoritative America/New_York session phase.

    The regular-session open/close comes from the NYSE calendar, so a delayed
    open, half day, holiday and DST transition are all handled without hard
    coding Beijing time.  The configured active window is 09:30-12:00 ET with
    an independent 11:59:30 submission cutoff.
    """
    stamp = _utc(now)
    now_utc = stamp.to_pydatetime()
    now_et = stamp.tz_convert("America/New_York").to_pydatetime()
    day = now_et.date()
    schedule = CALENDAR.schedule(start_date=day, end_date=day)
    if schedule.empty:
        return SessionInfo(
            SessionPhase.NO_SESSION, day.isoformat(), now_utc, now_et,
            None, None, None, None, None, False, False, False, "not_a_trading_day",
        )
    open_ts = schedule.iloc[0]["market_open"]
    close_ts = schedule.iloc[0]["market_close"]
    opening = open_ts.to_pydatetime()
    closing = close_ts.to_pydatetime()
    premarket_start_ts = _et(day, _clock_time(getattr(execution, "premarket_start", None), "09:00"))
    active_start_ts = _et(day, _clock_time(getattr(execution, "active_start", None), "09:30"))
    active_end_ts = _et(day, _clock_time(getattr(execution, "active_end", None), "12:00"))
    submit_cutoff_ts = _et(day, _clock_time(getattr(execution, "submit_cutoff", None), "11:59:30"))
    # A delayed exchange open is not tradable before the real open.
    active_start_ts = max(active_start_ts, opening)
    premarket = premarket_start_ts <= stamp < active_start_ts
    active = active_start_ts <= stamp < active_end_ts
    can_submit = active and stamp < submit_cutoff_ts
    if stamp < premarket_start_ts:
        phase, reason = SessionPhase.CLOSED, "before_premarket"
    elif premarket:
        phase, reason = SessionPhase.PREMARKET, "premarket_research"
    elif active:
        phase, reason = SessionPhase.ACTIVE, "active_window"
    else:
        phase, reason = SessionPhase.CLOSED, "after_active_window"
    return SessionInfo(
        phase=phase,
        trading_day=day.isoformat(),
        now_utc=now_utc,
        now_et=now_et,
        market_open=opening,
        market_close=closing,
        premarket_start=premarket_start_ts,
        active_end=active_end_ts,
        submit_cutoff=submit_cutoff_ts,
        can_submit=can_submit,
        active=active,
        premarket=premarket,
        reason=reason,
    )


def opening_minutes(now):
    now = pd.Timestamp(now)
    date = now.tz_convert("America/New_York").date()
    schedule = CALENDAR.schedule(date, date)
    if schedule.empty:
        return None
    return (now - schedule.iloc[0].market_open).total_seconds() / 60


def execution_open(now, execution=None):
    """Compatibility cache helper; true throughout the configured active window."""
    info = session_info(now, execution)
    return info.active


def validate_bars(df, end, now, risk, count):
    if not isinstance(df.index, pd.DatetimeIndex) or df.index.tz is None:
        raise ValueError("Missing bar timezone")
    df = df.copy().sort_index()
    if df.index.has_duplicates:
        raise ValueError("Duplicate bars")
    df = df.loc[df.index + pd.Timedelta(minutes=5) <= end]
    if len(df) < count or df.index[-1] + pd.Timedelta(minutes=5) != end:
        raise ValueError(
            f"Missing completed bars: required={count}, available={len(df)}, "
            f"missing_count={max(0,count-len(df))}, expected_end={end}, "
            f"latest_start={df.index[-1] if len(df) else None}"
        )
    if not 0 <= (pd.Timestamp(now) - end).total_seconds() <= risk.max_bar_age_seconds:
        raise ValueError("Stale bars")
    df = df.tail(count)
    values = df[["open", "high", "low", "close", "volume"]].to_numpy(dtype=float)
    if not np.isfinite(values).all() or (values[:, :4] <= 0).any() or (values[:, 4] < 0).any():
        raise ValueError("Invalid OHLCV")
    if ((df.high < df[["open", "close", "low"]].max(axis=1)) | (df.low > df[["open", "close", "high"]].min(axis=1))).any():
        raise ValueError("Invalid OHLC range")
    schedule = CALENDAR.schedule(df.index[0].date(), df.index[-1].date())
    valid = pd.DatetimeIndex([], tz="UTC")
    for _, row in schedule.iterrows():
        valid = valid.union(pd.date_range(row.market_open, row.market_close - pd.Timedelta(minutes=5), freq="5min"))
    expected = valid[(valid >= df.index[0]) & (valid <= df.index[-1])]
    if len(expected) != len(df) or not df.index.isin(expected).all():
        raise ValueError(
            f"Missing or misaligned regular-session bars: "
            f"missing_count={len(expected.difference(df.index))}, "
            f"missing={list(expected.difference(df.index)[:10])}, "
            f"unexpected={list(df.index.difference(expected)[:10])}"
        )
    return df[["open", "high", "low", "close", "volume"]]


def indicators(df, names):
    result = {"close": float(df.close.iloc[-1])}
    for name in names:
        match = re.fullmatch(r"(sma|rsi)_(\d+)", name)
        if not match:
            raise ValueError("Unsupported indicator")
        kind, period = match[1], int(match[2])
        if period < 2 or len(df) < period + 1:
            raise ValueError("Insufficient indicator history")
        if kind == "sma":
            value = df.close.tail(period).mean()
        else:
            delta = df.close.diff().tail(period)
            gain = delta.clip(lower=0).mean()
            loss = -delta.clip(upper=0).mean()
            value = 50 if gain == loss == 0 else (100 if loss == 0 else 100 - 100 / (1 + gain / loss))
        result[name] = float(value)
    return result


def daily_history(df, now):
    if df.index.tz is None or df.index.has_duplicates:
        raise ValueError("Invalid daily timestamps")
    today = pd.Timestamp(now).tz_convert("America/New_York").date()
    df = df.sort_index()
    df = df.loc[df.index.tz_convert("America/New_York").date < today]
    schedule = CALENDAR.schedule(pd.Timestamp(today) - pd.DateOffset(months=6), pd.Timestamp(today) - pd.Timedelta(days=1))
    first = schedule.index[0].date()
    df = df.loc[df.index.tz_convert("America/New_York").date >= first]
    actual = pd.Index(df.index.tz_convert("America/New_York").date)
    expected = pd.Index(schedule.index.date)
    if actual.has_duplicates or len(actual) != len(expected) or not actual.equals(expected):
        raise ValueError(
            f"Stale or insufficient daily history: required={len(expected)}, "
            f"available={len(actual)}, missing_count={len(expected.difference(actual))}, "
            f"missing={list(expected.difference(actual)[:10])}"
        )
    v = df[["open", "high", "low", "close", "volume"]].to_numpy(float)
    if not np.isfinite(v).all() or (v[:, :4] <= 0).any() or (v[:, 4] < 0).any():
        raise ValueError("Invalid daily bars")
    if ((df.high < df[["open", "close", "low"]].max(axis=1)) | (df.low > df[["open", "close", "high"]].min(axis=1))).any():
        raise ValueError("Invalid daily OHLC range")
    return df[["open", "high", "low", "close", "volume"]]


def _records(frame: pd.DataFrame) -> list[dict]:
    if frame is None or frame.empty:
        return []
    out = []
    for stamp, row in frame.iterrows():
        out.append(
            {
                "timestamp": pd.Timestamp(stamp).isoformat(),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row["volume"]),
            }
        )
    return out


def compact_history(daily: pd.DataFrame, intraday: pd.DataFrame | None = None) -> dict:
    """Token-bounded but still raw-structured history for AI research.

    Daily bars are kept for the recent stretch and downsampled to weekly and
    monthly OHLCV for the older context.  This preserves the actual price
    structure (not just indicators) while avoiding re-sending ~6 months of
    unchanged daily rows on every call.
    """
    daily = daily.copy().sort_index()
    recent = daily.tail(60)
    weekly = daily.resample("W-FRI").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna().tail(26)
    monthly = daily.resample("ME").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna().tail(12)
    result = {
        "daily_recent": _records(recent),
        "weekly": _records(weekly),
        "monthly": _records(monthly),
        "daily_rows_total": len(daily),
    }
    if intraday is not None:
        result["intraday_recent"] = _records(intraday.tail(78))
    return result


def quote_fields(quote: dict) -> dict:
    """Only expose trigger fields that actually exist with a timestamp."""
    fields = {}
    for name in ("last", "ask", "bid"):
        value = quote.get(name)
        if value is not None and np.isfinite(float(value)) and float(value) > 0:
            fields[name] = float(value)
    if fields.get("bid") and fields.get("ask"):
        fields["spread"] = fields["ask"] - fields["bid"]
        fields["spread_bps"] = (fields["ask"] - fields["bid"]) / fields["bid"] * 10000
    if quote.get("timestamp"):
        fields["quote_timestamp"] = quote["timestamp"]
    if quote.get("last_timestamp"):
        fields["last_timestamp"] = quote["last_timestamp"]
    if quote.get("source"):
        fields["feed"] = quote["source"]
    return fields
