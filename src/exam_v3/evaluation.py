"""Deterministic benchmark evaluation with explicit intraday timing."""
from datetime import time
from exam_data.store import ET

def qqq_session_prices(conn, day):
    """First regular-session open and last regular-session close (not 08:xx)."""
    first = conn.execute(
        "SELECT ts,open FROM bars WHERE symbol='QQQ' AND timeframe='1Min' AND feed='iex' "
        "AND day=? AND ts>=? ORDER BY ts LIMIT 1", [day, __import__('datetime').datetime.combine(day,time(9,30),ET)]
    ).fetchone()
    last = conn.execute(
        "SELECT end_at,close FROM bars WHERE symbol='QQQ' AND timeframe='1Min' AND feed='iex' "
        "AND day=? AND end_at<=? ORDER BY end_at DESC LIMIT 1", [day, __import__('datetime').datetime.combine(day,time(16),ET)]
    ).fetchone()
    if not first or not last: raise ValueError('QQQ regular-session bars unavailable')
    return {'first_ts': first[0], 'first_open': float(first[1]),
            'last_ts': last[0], 'last_close': float(last[1])}

def qqq_buy_hold(conn, days):
    if not days: raise ValueError('No days')
    first = qqq_session_prices(conn, days[0])
    last = qqq_session_prices(conn, days[-1])
    return last['last_close']/first['first_open']-1

def exposure_matched(exposure_points, qqq_prices):
    """Compound QQQ returns using exposure/equity observed between points.

    exposure_points: ascending [(timestamp, equity, gross_exposure), ...]
    qqq_prices: ascending [(timestamp, price), ...] covering the same interval.
    The first exposure point starts at 1.0; no averaging over prior days.
    """
    if len(exposure_points) < 2: raise ValueError('At least two exposure points')
    if len(qqq_prices) < 2: raise ValueError('At least two QQQ prices')
    def price_at(ts):
        candidates=[p for t,p in qqq_prices if t<=ts]
        return candidates[-1] if candidates else None
    value=1.0
    for i in range(len(exposure_points)-1):
        t0,e0,g0=exposure_points[i]; t1,e1,g1=exposure_points[i+1]
        if t1<=t0: raise ValueError('Exposure timestamps must increase')
        p0,p1=price_at(t0),price_at(t1)
        if p0 is None or p1 is None or p0<=0: continue
        ratio=g0/e0 if e0 else 0.0
        value*=1.0+ratio*(p1/p0-1)
    return value-1.0
