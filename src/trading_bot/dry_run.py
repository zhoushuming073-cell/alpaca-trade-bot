from datetime import datetime, timezone
from .decision_schema import Decision
from .engine import Engine
from .settings import Policy, Check

class FakeBroker:
    def __init__(self, outcome="filled"):
        self.orders={}
        self.submissions=0
        self.open=True
        self.outcome=outcome
        self.positions={}
        self.cash=100000.
        self.snapshot_calls=0
        self.clock_calls=0
        self.cancel_calls=[]
    def snapshot(self):
        from .storage import TERMINAL
        self.snapshot_calls+=1
        return {"equity":self.cash+sum(q*100 for q in self.positions.values()),"last_equity":100000.,"cash":self.cash,"buying_power":self.cash,"positions":{s:q*100 for s,q in self.positions.items()},"quantities":dict(self.positions),"open_orders":[dict(o) for o in self.orders.values() if o["status"] not in TERMINAL],"blocked":False,"asof":"synthetic"}
    def is_open(self):
        self.clock_calls+=1
        return self.open
    def quote(self,*args): return {"ask":100.,"bid":100.,"source":"synthetic"}
    def lookup(self,cid): return self.orders.get(cid)
    def submit(self,intent,cid):
        from .safety import validate_order
        validate_order(intent)
        if cid in self.orders:
            raise AssertionError("Duplicate POST")
        self.submissions+=1
        qty=intent["quantity"] if self.outcome=="filled" else (min(2,intent["quantity"]) if self.outcome=="partial" else 0)
        status="filled" if self.outcome=="filled" else ("rejected" if self.outcome=="rejected" else "canceled")
        self.orders[cid]={"id":"fake-"+cid,"client_order_id":cid,"symbol":intent["symbol"],"side":intent["side"],"qty":str(intent["quantity"]),"type":"limit","time_in_force":"ioc","status":status,"filled_qty":str(qty),"filled_avg_price":str(intent["limit_price"]) if qty else None,"synthetic":True}
        signed=qty if intent["side"]=="buy" else -qty
        self.positions[intent["symbol"]]=self.positions.get(intent["symbol"],0)+signed
        self.cash-=signed*intent["limit_price"]
        return dict(self.orders[cid])
    def cancel(self,cid):
        self.cancel_calls.append(cid)
        if cid in self.orders:
            self.orders[cid]["status"]="canceled"

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
