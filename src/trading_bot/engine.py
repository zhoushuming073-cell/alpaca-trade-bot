import hashlib
import json
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from .market_data import window, validate_bars, indicators, execution_open, opening_minutes
from .risk_manager import plan
from .execution import reconcile, execute

class Engine:
    def __init__(self, store, broker, ai, symbols, policy, risk, data, mode="paper", clock=None, daily_data=None):
        self.store,self.broker,self.ai=store,broker,ai
        self.symbols,self.policy,self.risk,self.data=symbols,policy,risk,data
        self.mode=mode
        self.daily_data=daily_data
        self.clock=clock or (lambda: datetime.now(timezone.utc))

    def tick(self):
        cycle=None
        try:
            account=reconcile(self.store,self.broker)
            now=self.clock(); end=window(now)
            minutes=opening_minutes(now)
            if minutes is not None and 29.5<=minutes<30:
                for cid in self.store.pending():
                    self.broker.cancel(cid)
                reconcile(self.store,self.broker)
                self.store.event("cutoff_cleanup",{"unresolved":self.store.pending()})
                return "cutoff"
            self.store.event("heartbeat",{"mode":self.mode,"market_open":self.broker.is_open(),"paused":self.store.paused()})
            if end is None or not self.broker.is_open():
                return "closed"
            if not execution_open(now):
                if self.store.pending():
                    self.store.event("critical_unresolved_after_cutoff",{"orders":self.store.pending()})
                return "outside_window"
            if self.store.paused() or not self.policy.ready() or not self.symbols:
                return "disabled"
            if self.store.pending() or account["open_orders"]:
                return "pending"
            cycle=end.isoformat()
            if not self.store.claim(cycle):
                return "duplicate"
            frames={s:validate_bars(self.data(s),end,now,self.risk,self.policy.history_bars) for s in self.symbols}
            metrics={s:indicators(df,self.policy.indicators) for s,df in frames.items()}
            payload={"symbols":self.symbols,"bar_end":cycle,"source":"synthetic" if self.mode=="dry-run" else "iex","strategy":self.policy.model_dump(),"risk":self.risk.model_dump(),"account":account,"indicators":metrics,"bars":{s:df.reset_index().to_dict("records") for s,df in frames.items()}}
            payload["config_hash"]=hashlib.sha256(json.dumps([self.symbols,self.policy.model_dump(),self.risk.model_dump()],sort_keys=True).encode()).hexdigest()
            payload["daily_history_scope"]={"months":6,"adjustment":"split","intraday_adjustment":"raw"}
            payload["unavailable_context"]=["news", "sector_performance", "premarket_change"]
            if self.daily_data:
                from .market_data import daily_history
                payload["daily_bars"]={s:daily_history(self.daily_data(s),now).reset_index().to_dict("records") for s in self.symbols}
            self.store.event("input",payload)
            decisions,meta=self.ai.decide(payload)
            from .decision_schema import parse
            decisions=parse(json.dumps({"decisions":[d.model_dump() for d in decisions]}),self.symbols)
            self.store.event("decision",{"decisions":[d.model_dump() for d in decisions],**meta})
            for d in decisions:
                if d.action=="HOLD":
                    continue
                now=self.clock()
                if not execution_open(now) or window(now)!=end or (now-end).total_seconds()>self.risk.max_bar_age_seconds or not self.broker.is_open():
                    raise ValueError("Decision expired")
                account=reconcile(self.store,self.broker)
                if self.store.pending():
                    break
                quote=self.broker.quote(d.symbol,self.risk,metrics[d.symbol]["close"])
                self.store.event("quote",{"symbol":d.symbol,**quote})
                day=now.astimezone(ZoneInfo("America/New_York")).date().isoformat()
                try:
                    intent=plan(d,account,quote,metrics[d.symbol],self.policy,self.risk,self.symbols,self.store.count(day),self.store.paused())
                except ValueError as exc:
                    self.store.event("risk_rejected",{"symbol":d.symbol,"reason":str(exc)})
                    continue
                if intent:
                    if self.store.paused():
                        raise ValueError("Paused before submission")
                    execute(self.store,self.broker,cycle,day,intent)
            self.store.finish(cycle,"complete")
            return "complete"
        except Exception as exc:
            # Do not persist provider exception bodies: these can contain credentials.
            self.store.event("error",{"type":type(exc).__name__,"cycle":cycle})
            logging.error("Cycle failed safely: %s",type(exc).__name__)
            if cycle:
                self.store.finish(cycle,"failed")
            return "failed"
