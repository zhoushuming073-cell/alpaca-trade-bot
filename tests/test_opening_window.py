from datetime import datetime,timezone
import pytest
from trading_bot.market_data import execution_open
from test_system import engine, BuyAI

@pytest.mark.parametrize("time,allowed",[("2026-11-04T14:45:00+00:00",True),("2026-11-04T14:59:30+00:00",True),("2026-11-04T15:00:00+00:00",False),("2026-07-06T13:45:00+00:00",True),("2026-07-06T14:00:00+00:00",False)])
def test_window(time,allowed):
    assert execution_open(datetime.fromisoformat(time))==allowed

def test_cutoff_cancel(store,risk):
    e=engine(store,risk,BuyAI())
    store.reserve("test","2026-11-02",{})
    e.broker.orders["test"]={"status":"new"}
    store.pause(True)
    calls=[]
    def cancel(cid):
        calls.append(cid)
        e.broker.orders[cid]={"status":"canceled"}
    e.broker.cancel=cancel
    e.clock=lambda:datetime(2026,11,2,14,59,30,tzinfo=timezone.utc)
    assert e.tick()=="disabled"
    assert calls==["test"] and not store.pending()

def test_no_mutations_after_cutoff(store,risk):
    e=engine(store,risk,BuyAI())
    e.clock=lambda:datetime(2026,11,2,15,0,tzinfo=timezone.utc)
    assert e.tick()=="outside_window" and e.broker.submissions==0
