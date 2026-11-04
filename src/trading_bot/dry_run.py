from datetime import datetime, timezone
from .decision_schema import Decision
from .engine import Engine
from .settings import Policy, Check

class FakeBroker:
    def __init__(self):
        self.orders={}
        self.submissions=0
        self.open=True
    def snapshot(self):
        return {"equity":100000.,"last_equity":100000.,"cash":100000.,"buying_power":100000.,"positions":{},"quantities":{},"open_orders":[],"blocked":False,"asof":"synthetic"}
    def is_open(self): return self.open
    def quote(self,*args): return {"ask":100.,"bid":100.,"source":"synthetic"}
    def lookup(self,cid): return self.orders.get(cid)
    def submit(self,intent,cid):
        self.submissions+=1
        self.orders[cid]={"client_order_id":cid,"status":"filled","filled_qty":str(intent["quantity"]),"filled_avg_price":str(intent["limit_price"]),"synthetic":True}

class HoldAI:
    def decide(self,payload):
        return [Decision(symbol=s,action="HOLD",target_weight=0,confidence=0,reason="Offline plumbing fixture; no investment signal",risk_assessment="synthetic") for s in payload["symbols"]],{"model":"offline-fixture","latency_seconds":0}

def fixture_policy():
    return Policy(enabled=True,version="TEST-ONLY",entry_rules="Test fixture",exit_rules="Test fixture",analysis_rules="Test fixture",history_bars=2,entry_checks=[Check(indicator="close",op="gt",value=0)],exit_checks=[Check(indicator="close",op="gt",value=0)])

def bars():
    import pandas as pd
    return pd.DataFrame({"open":[100.,100.],"high":[101.,101.],"low":[99.,99.],"close":[100.,100.],"volume":[10,10]},index=pd.to_datetime(["2026-10-30T19:55Z","2026-11-02T14:30Z"]))

def run(store,risk):
    engine=Engine(store,FakeBroker(),HoldAI(),["TEST"],fixture_policy(),risk,lambda s:bars(),mode="dry-run",clock=lambda:datetime(2026,11,2,14,35,10,tzinfo=timezone.utc))
    return engine.tick()
