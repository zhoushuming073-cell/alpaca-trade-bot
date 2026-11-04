import pandas as pd
import pytest
from trading_bot.market_data import CALENDAR,daily_history,window,execution_open

def history():
    dates=CALENDAR.schedule("2026-04-01","2026-11-03").index.tz_localize("America/New_York")
    return pd.DataFrame({"open":100.,"high":101.,"low":99.,"close":100.,"volume":10},index=dates)

def test_six_calendar_months():
    result=daily_history(history(),pd.Timestamp("2026-11-04T15:00Z"))
    assert result.index[0].date().isoformat()=="2026-05-04"
    assert len(result)>120
    assert result.index[-1].date().isoformat()=="2026-11-03"

def test_short_history_rejected():
    with pytest.raises(ValueError): daily_history(history().tail(30),pd.Timestamp("2026-11-04T15:00Z"))

def test_exactly_five_opening_slots():
    opening=pd.Timestamp("2026-11-04T14:30Z")
    slots=[]
    for minute in (5,10,15,20,25,30):
        now=opening+pd.Timedelta(minutes=minute,seconds=10)
        if window(now) is not None and execution_open(now): slots.append(minute)
    assert slots==[5,10,15,20,25]
