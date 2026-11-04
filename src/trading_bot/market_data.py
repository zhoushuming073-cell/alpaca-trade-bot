import re
import numpy as np
import pandas as pd
import pandas_market_calendars as mcal

CALENDAR = mcal.get_calendar("NYSE")

def regular_bars(df):
    if df.empty:
        return df
    if df.index.tz is None:
        raise ValueError("Missing timezone")
    schedule=CALENDAR.schedule(df.index[0].date(),df.index[-1].date())
    mask=np.zeros(len(df),dtype=bool)
    for _,row in schedule.iterrows():
        mask |= (df.index>=row.market_open) & (df.index+pd.Timedelta(minutes=5)<=row.market_close)
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

def validate_bars(df, end, now, risk, count):
    if not isinstance(df.index, pd.DatetimeIndex) or df.index.tz is None:
        raise ValueError("Missing bar timezone")
    df = df.copy().sort_index()
    if df.index.has_duplicates:
        raise ValueError("Duplicate bars")
    df = df.loc[df.index + pd.Timedelta(minutes=5) <= end]
    if len(df) < count or df.index[-1] + pd.Timedelta(minutes=5) != end:
        raise ValueError("Missing completed bars")
    if not 0 <= (pd.Timestamp(now)-end).total_seconds() <= risk.max_bar_age_seconds:
        raise ValueError("Stale bars")
    df = df.tail(count)
    values = df[["open","high","low","close","volume"]].to_numpy(dtype=float)
    if not np.isfinite(values).all() or (values[:,:4] <= 0).any() or (values[:,4]<0).any():
        raise ValueError("Invalid OHLCV")
    if ((df.high < df[["open","close","low"]].max(axis=1)) | (df.low > df[["open","close","high"]].min(axis=1))).any():
        raise ValueError("Invalid OHLC range")
    # Every row must be an exchange-session-aligned regular-hours bar.
    schedule = CALENDAR.schedule(df.index[0].date(), df.index[-1].date())
    valid = pd.DatetimeIndex([], tz="UTC")
    for _, row in schedule.iterrows():
        valid = valid.union(pd.date_range(row.market_open, row.market_close-pd.Timedelta(minutes=5), freq="5min"))
    expected = valid[(valid >= df.index[0]) & (valid <= df.index[-1])]
    if len(expected) != len(df) or not df.index.isin(expected).all():
        raise ValueError("Missing or misaligned regular-session bars")
    return df[["open","high","low","close","volume"]]

def indicators(df, names):
    result = {"close":float(df.close.iloc[-1])}
    for name in names:
        match = re.fullmatch(r"(sma|rsi)_(\d+)", name)
        if not match:
            raise ValueError("Unsupported indicator")
        kind, period = match[1], int(match[2])
        if period < 2 or len(df) < period+1:
            raise ValueError("Insufficient indicator history")
        if kind == "sma":
            value = df.close.tail(period).mean()
        else:
            delta=df.close.diff().tail(period)
            gain=delta.clip(lower=0).mean(); loss=-delta.clip(upper=0).mean()
            value=50 if gain==loss==0 else (100 if loss==0 else 100-100/(1+gain/loss))
        result[name]=float(value)
    return result


def opening_minutes(now):
    now=pd.Timestamp(now)
    date=now.tz_convert("America/New_York").date()
    schedule=CALENDAR.schedule(date,date)
    if schedule.empty:
        return None
    return (now-schedule.iloc[0].market_open).total_seconds()/60

def execution_open(now):
    minutes=opening_minutes(now)
    return minutes is not None and 0<=minutes<29.5

def daily_history(df, now):
    if df.index.tz is None or df.index.has_duplicates:
        raise ValueError("Invalid daily timestamps")
    today=pd.Timestamp(now).tz_convert("America/New_York").date()
    df=df.loc[df.index.tz_convert("America/New_York").date<today]
    schedule=CALENDAR.schedule(pd.Timestamp(today)-pd.DateOffset(months=6),pd.Timestamp(today)-pd.Timedelta(days=1))
    first=schedule.index[0].date()
    df=df.loc[df.index.tz_convert("America/New_York").date>=first]
    if len(df)<len(schedule) or df.index[0].tz_convert("America/New_York").date()!=first or df.index[-1].tz_convert("America/New_York").date()!=schedule.index[-1].date():
        raise ValueError("Stale or insufficient daily history")
    v=df[["open","high","low","close","volume"]].to_numpy(float)
    if not np.isfinite(v).all() or (v[:,:4]<=0).any() or (v[:,4]<0).any():
        raise ValueError("Invalid daily bars")
    return df[["open","high","low","close","volume"]]
