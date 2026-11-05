import json
from datetime import datetime,timezone
from types import SimpleNamespace
import pytest
from trading_bot.decision_schema import parse,Decision
from trading_bot.dry_run import FakeBroker,HoldAI,bars,fixture_policy
from trading_bot.engine import Engine
from trading_bot.market_data import window,validate_bars
from trading_bot.execution import execute,reconcile
from trading_bot.risk_manager import plan

def decision(action="BUY",weight=.05,symbol="TEST"):
    return Decision(symbol=symbol,action=action,target_weight=weight,confidence=.5,reason="fixture",risk_assessment="test")

@pytest.mark.parametrize("action",["BUY","SELL","HOLD"])
def test_parse(action):
    assert parse(json.dumps({"decisions":[decision(action).model_dump()]}),["TEST"])[0].action==action

@pytest.mark.parametrize("raw",["", "garbage", "{}", '{"decisions":[]}', '{"decisions":[{"symbol":"OTHER"}]}'])
def test_invalid_json(raw):
    with pytest.raises(ValueError): parse(raw,["TEST"])

@pytest.mark.parametrize("field,value",[("target_weight",float("nan")),("target_weight",1.1),("confidence",-1),("action","SHORT")])
def test_bad_schema(field,value):
    x=decision().model_dump();x[field]=value
    with pytest.raises(ValueError): Decision(**x)

def engine(store,risk,ai=None):
    return Engine(store,FakeBroker(),ai or HoldAI(),["TEST"],fixture_policy(),risk,lambda s:bars(),clock=lambda:datetime(2026,11,2,14,35,10,tzinfo=timezone.utc))

class BuyAI:
    def decide(self,payload): return [decision()],{"model":"fixture"}

def test_duplicate_cycle(store,risk):
    e=engine(store,risk,BuyAI())
    assert e.tick()=="complete"
    assert e.broker.submissions==1
    assert e.tick()=="duplicate"
    assert e.broker.submissions==1

def test_closed(store,risk):
    e=engine(store,risk,BuyAI());e.broker.open=False
    assert e.tick()=="closed" and e.broker.submissions==0

def test_disabled(store,risk):
    e=engine(store,risk,BuyAI());e.policy.enabled=False
    assert e.tick()=="disabled" and e.broker.submissions==0

def test_pause(store,risk):
    e=engine(store,risk,BuyAI());store.pause(True)
    assert e.tick()=="disabled" and e.broker.submissions==0

def test_stale(store,risk):
    e=engine(store,risk,BuyAI());e.clock=lambda:datetime(2026,11,2,14,38,tzinfo=timezone.utc)
    assert e.tick()=="skipped_no_valid_data" and e.broker.submissions==0

@pytest.mark.parametrize("error",[ValueError("bad JSON"),TimeoutError("offline")])
def test_ai_failure(store,risk,error):
    class Fail:
        def decide(self,payload): raise error
    e=engine(store,risk,Fail())
    assert e.tick()=="failed" and e.broker.submissions==0

@pytest.mark.parametrize("change",["outside","weight","total","cash","loss","count","orders","condition","paused","short"])
def test_risk_rejects(risk,change):
    d=decision();a=FakeBroker().snapshot();p=fixture_policy();count=0;paused=False
    if change=="outside": d.symbol="OTHER"
    if change=="weight": d.target_weight=.2
    if change=="total": a["positions"]={"OTHER":49000}
    if change=="cash": a["cash"]=1
    if change=="loss": a["equity"]=97000
    if change=="count": count=10
    if change=="orders": a["open_orders"]=[{}]
    if change=="condition": p.entry_checks[0].value=200
    if change=="paused": paused=True
    if change=="short": a["positions"]={"OTHER":-100}
    with pytest.raises(ValueError): plan(d,a,{"bid":100,"ask":100},{"close":100},p,risk,["TEST"],count,paused)

def test_sell(risk):
    a=FakeBroker().snapshot();a["positions"]={"TEST":5000};a["quantities"]={"TEST":50}
    intent=plan(decision("SELL",0),a,{"bid":100,"ask":100},{"close":100},fixture_policy(),risk,["TEST"],0,False)
    assert intent["quantity"]==50

def test_timeout_reconcile(store,tmp_path):
    class Timeout(FakeBroker):
        def submit(self,intent,cid):
            super().submit(intent,cid)
            raise TimeoutError()
    b=Timeout();cid=execute(store,b,"cycle","2026-11-02",{"symbol":"TEST","quantity":1,"side":"buy","limit_price":100})
    assert not store.pending() and b.submissions==1
    from trading_bot.storage import Store
    restarted=Store(tmp_path/"test.sqlite")
    assert reconcile(restarted,b)["equity"]==100000
    assert restarted.count("2026-11-02")==1
    restarted.db.close()

def test_unknown_blocks_future(store,risk):
    e=engine(store,risk,BuyAI());store.reserve("unknown","2026-11-02",{})
    assert e.tick()=="pending" and e.broker.submissions==0

@pytest.mark.parametrize("time,expected",[("2026-11-07T15:00Z",None),("2026-11-26T15:00Z",None),("2026-11-27T18:00Z",None),("2026-11-27T17:45:10Z","2026-11-27T17:45:00+00:00"),("2026-07-06T13:45:10Z","2026-07-06T13:45:00+00:00"),("2026-11-02T14:34Z",None)])
def test_calendar(time,expected):
    import pandas as pd
    w=window(pd.Timestamp(time))
    assert (w.isoformat() if w is not None else None)==expected

def test_incomplete_bar_removed(risk):
    import pandas as pd
    df=bars();df.loc[pd.Timestamp("2026-11-02T14:35Z")]=[100,101,99,100,1]
    assert len(validate_bars(df,pd.Timestamp("2026-11-02T14:35Z"),pd.Timestamp("2026-11-02T14:35:10Z"),risk,2))==2

def test_deepseek_sdk(risk):
    from trading_bot.deepseek_client import DeepSeek
    calls=[]
    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(model="fixture",choices=[SimpleNamespace(finish_reason="stop",message=SimpleNamespace(content=json.dumps({"decisions":[decision("HOLD").model_dump()]})))])
    fake=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    assert DeepSeek("fake",client=fake).decide({"symbols":["TEST"]})[0][0].action=="HOLD"
    assert calls[0]["response_format"]=={"type":"json_object"}

def test_paper_config_rejects_live(monkeypatch):
    from trading_bot.settings import credentials
    monkeypatch.setenv("ALPACA_IS_PAPER","false")
    with pytest.raises(ValueError): credentials()

def test_lumibot_contract():
    from lumibot.data_sources import AlpacaData
    from lumibot.strategies import Strategy
    from trading_bot.strategy import PaperStrategy
    import inspect
    data=AlpacaData({"API_KEY":"fake","API_SECRET":"fake","PAPER":True},delay=0)
    tf=data._parse_source_timestep("5 minutes",reverse=True)
    assert tf.amount==5
    assert issubclass(PaperStrategy,Strategy)
    assert "custom_params" in inspect.signature(Strategy.create_order).parameters
