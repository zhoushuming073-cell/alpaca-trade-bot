"""Deterministic isolated simulator with one shared quantity per protective group."""
from dataclasses import dataclass
from datetime import datetime,timedelta
from decimal import Decimal
from .contracts import Decision,confirmed,time_state
from exam_data.store import ET

D=lambda x:Decimal(str(x))
@dataclass
class Order:
    decision:Decision
    at:datetime
    filled:int=0
    status:str='NEW'

class Broker:
    def __init__(self,symbols,cash=100000,*,fee='0.005',slippage_bps=5,participation='.1',short_locates=None):
        self.symbols=set(symbols);self.cash=D(cash);self.initial_cash=D(cash)
        self.fee=D(fee);self.slip=D(slippage_bps)/10000;self.participation=D(participation)
        self.short_locates=short_locates or {};self.positions={};self.avg={};self.proceeds={}
        self.orders={};self.groups={};self.fills=[];self.events=[];self.last_bar={};self.last_borrow_day=None;self.prior_short_value=D(0)
        self.entry_at={};self.realized=D(0);self.fees=D(0);self.borrow_cost=D(0)
    def event(self,kind,at,**data):self.events.append({'kind':kind,'at':at.isoformat(),**data})
    def reserved(self):
        return sum(((o.decision.limit_price*(D('.5') if o.decision.action=='OPEN_SHORT' else 1)+self.fee)*(o.decision.qty-o.filled)
                    for o in self.orders.values() if o.status in {'NEW','PARTIALLY_FILLED'} and o.decision.action in {'OPEN_LONG','OPEN_SHORT'}),D(0))
    def free(self):return self.cash-D('1.5')*sum(self.proceeds.values(),D(0))-self.reserved()
    def submit(self,decision,at,observed_price):
        if not time_state(at)['new_order_allowed']:raise ValueError('Window closed')
        if decision.symbol not in self.symbols or decision.decision_id in self.orders:raise ValueError('Unknown symbol or duplicate decision')
        if at>=decision.valid_until:raise ValueError('Expired plan')
        if not confirmed(decision,observed_price):raise ValueError('Confirmation not met')
        s=decision.symbol;held=self.positions.get(s,0);action=decision.action
        if action in {'HOLD','WAIT'}:return None
        if action in {'PROTECT','REMOVE_PROTECTION'}:
            if not held:raise ValueError('No position')
            if action=='REMOVE_PROTECTION':self.groups.pop(s,None)
            else:self.protect(s,decision.stop_price,decision.take_profit,at,decision.protection_tif)
            self.orders[decision.decision_id]=Order(decision,at,status='APPLIED');return None
        if any(o.decision.symbol==s and o.status in {'NEW','PARTIALLY_FILLED'} for o in self.orders.values()):raise ValueError('Pending order conflict')
        if action in {'CLOSE_LONG','COVER_SHORT','REDUCE'}:
            if not held or decision.qty>abs(held):raise ValueError('Cannot cross zero')
            if action=='CLOSE_LONG' and held<0 or action=='COVER_SHORT' and held>0:raise ValueError('Direction mismatch')
            # Pending close owns only its reserved quantity; the linked group
            # continues to protect the unreserved remainder.
        else:
            if action=='OPEN_LONG' and held<0 or action=='OPEN_SHORT' and held>0:raise ValueError('Cannot cross zero')
            if action=='OPEN_SHORT' and self.short_locates.get((at.astimezone(ET).date(),s)) is not True:raise ValueError('No dated short locate')
            needed=(decision.limit_price*(D('.5') if action=='OPEN_SHORT' else 1)+self.fee)*decision.qty
            if needed>self.free():raise ValueError('Insufficient unreserved capital; no resizing')
        self.orders[decision.decision_id]=Order(decision,at)
        self.event('submitted',at,id=decision.decision_id,symbol=s,qty=decision.qty,action=action)
        return self.orders[decision.decision_id]
    def protect(self,s,stop,target,at,tif='gtc'):
        held=self.positions.get(s,0)
        if not held or stop is None or D(stop)<=0:raise ValueError('Invalid protection')
        if target is not None and ((held>0 and D(target)<=D(stop)) or (held<0 and D(target)>=D(stop))):raise ValueError('Inconsistent linked prices')
        self.groups[s]={'stop':D(stop),'target':D(target) if target else None,'at':at,'tif':tif,'triggered':None}
        self.event('linked_protection',at,symbol=s,qty=abs(held))
    def fill(self,s,delta,price,at,order_id,reason):
        prior=self.positions.get(s,0);price=D(price);qty=abs(delta);fee=qty*self.fee
        if prior and (delta>0)!=(prior>0):
            if qty>abs(prior):raise ValueError('Fill crosses zero')
            self.realized+=(price-self.avg[s])*qty*(1 if prior>0 else -1)
            if prior<0:self.proceeds[s]=self.proceeds.get(s,D(0))*D(abs(prior)-qty)/abs(prior)
        else:
            self.avg[s]=(self.avg.get(s,D(0))*abs(prior)+price*qty)/(abs(prior)+qty)
            if not prior:self.entry_at[s]=at
            if delta<0:self.proceeds[s]=self.proceeds.get(s,D(0))+qty*price
        self.cash-=delta*price+fee;self.fees+=fee;self.positions[s]=prior+delta
        if not self.positions[s]:
            self.positions.pop(s);self.avg.pop(s,None);self.proceeds.pop(s,None);self.groups.pop(s,None);self.entry_at.pop(s,None)
        self.fills.append({'symbol':s,'delta':delta,'qty':qty,'price':str(price),'fee':str(fee),'at':at.isoformat(),'order_id':order_id,'reason':reason})
    def process_bar(self,bar):
        if bar['feed']!='iex' or bar['timeframe']!='1Min':raise ValueError('IEX completed minute required')
        s=bar['symbol'];start=bar['ts'];end=bar['end_at']
        if start.tzinfo is None or end!=start+timedelta(minutes=1):raise ValueError('Invalid completed bar')
        if s in self.last_bar and start<=self.last_bar[s]:raise ValueError('Duplicate or out-of-order bar')
        self.last_bar[s]=start
        capacity=int(D(bar['volume'])*self.participation)
        g=self.groups.get(s);held=self.positions.get(s,0)
        close_reserved=sum(o.decision.qty-o.filled for o in self.orders.values() if o.decision.symbol==s
                           and o.decision.action in {'CLOSE_LONG','COVER_SHORT','REDUCE'} and o.status in {'NEW','PARTIALLY_FILLED'})
        protected_qty=max(0,abs(held)-close_reserved)
        if g and protected_qty and start>=g['at'] and capacity>0:
            stop_hit=D(bar['low'])<=g['stop'] if held>0 else D(bar['high'])>=g['stop']
            target_hit=g['target'] is not None and (D(bar['high'])>=g['target'] if held>0 else D(bar['low'])<=g['target'])
            # Unknown intraminute order: adverse stop wins if both levels hit.
            if g['triggered']=='STOP' or stop_hit:
                previously_triggered=g['triggered']=='STOP'
                g['triggered']='STOP'
                raw=D(bar['open']) if previously_triggered else min(D(bar['open']),g['stop']) if held>0 else max(D(bar['open']),g['stop'])
                price=raw*(1-self.slip if held>0 else 1+self.slip);reason='LINKED_STOP'
            elif target_hit:
                price=max(D(bar['open']),g['target']) if held>0 else min(D(bar['open']),g['target']);reason='LINKED_TARGET'
            else:price=None
            if price is not None:
                q=min(protected_qty,capacity);self.fill(s,-q if held>0 else q,price,end,'protection:'+s,reason);capacity-=q
        for oid,o in list(self.orders.items()):
            d=o.decision
            if d.symbol!=s or o.status not in {'NEW','PARTIALLY_FILLED'} or start<o.at or capacity<=0:continue
            if d.confirmation.persistence=='WHILE_VALID':
                c=d.confirmation
                if not confirmed(d,bar['open']) or (c.kind=='ABOVE' and D(bar['low'])<c.level) or (c.kind=='BELOW' and D(bar['high'])>c.level):
                    o.status='CANCELED';self.event('confirmation_lost',end,id=oid);continue
            held=self.positions.get(s,0)
            buy=d.action in {'OPEN_LONG','COVER_SHORT'} or (d.action=='REDUCE' and held<0)
            if buy and D(bar['low'])>d.limit_price or not buy and D(bar['high'])<d.limit_price:continue
            price=min(d.limit_price,D(bar['open'])*(1+self.slip)) if buy else max(d.limit_price,D(bar['open'])*(1-self.slip))
            qty=min(d.qty-o.filled,capacity)
            if d.action in {'CLOSE_LONG','COVER_SHORT','REDUCE'}:qty=min(qty,abs(held))
            if not qty:o.status='CANCELED';continue
            self.fill(s,qty if buy else -qty,price,end,oid,'LIMIT_FILL');o.filled+=qty;capacity-=qty
            o.status='FILLED' if o.filled==d.qty else 'PARTIALLY_FILLED'
            if o.status=='FILLED' and d.action in {'OPEN_LONG','OPEN_SHORT'} and d.stop_price:
                self.protect(s,d.stop_price,d.take_profit,end,d.protection_tif)
    def expire(self,day,at):
        for o in self.orders.values():
            if o.status in {'NEW','PARTIALLY_FILLED'} and o.at.astimezone(ET).date()<=day:o.status='EXPIRED'
        for s,g in list(self.groups.items()):
            if g['tif']=='day' and g['at'].astimezone(ET).date()<=day:self.groups.pop(s)
        self.event('day_expired',at)
    def accrue_borrow(self,day,marks,annual_rate=D('.08')):
        if self.last_borrow_day is not None and day<=self.last_borrow_day:raise ValueError('Duplicate borrow accrual')
        days=(day-self.last_borrow_day).days if self.last_borrow_day else 1
        current_value=sum((abs(q)*D(marks[s]) for s,q in self.positions.items() if q<0),D(0))
        # Non-session days use the last known close exposure; new Monday shorts
        # are not charged retroactively for the preceding weekend.
        cost=(current_value+self.prior_short_value*max(0,days-1))*annual_rate/365
        self.prior_short_value=current_value
        self.cash-=cost;self.borrow_cost+=cost;self.last_borrow_day=day;return cost
    def account(self,at,marks):
        if any(s not in marks for s in self.positions):raise ValueError('Missing position mark')
        equity=self.cash+sum((D(marks[s])*q for s,q in self.positions.items()),D(0))
        return {'source':'ISOLATED_SIMULATOR','asof_utc':at.isoformat(),'cash':float(self.cash),'equity':float(equity),
                'positions':dict(self.positions),'cost_basis':{s:float(v) for s,v in self.avg.items()},
                'gross_exposure':sum(abs(q*float(marks[s])) for s,q in self.positions.items()),
                'available_non_margin_cash':float(max(D(0),self.free())), 'reserved_capital':float(self.reserved()),
                'open_orders':[{'id':k,'symbol':o.decision.symbol,'remaining':o.decision.qty-o.filled,'notional':float(o.decision.limit_price*(o.decision.qty-o.filled)),'status':o.status} for k,o in self.orders.items() if o.status in {'NEW','PARTIALLY_FILLED'}],
                'protection':{s:{k:str(v) for k,v in g.items()} for s,g in self.groups.items()},
                'historical_short_locates':{s:self.short_locates.get((at.astimezone(ET).date(),s),'UNKNOWN') for s in sorted(self.symbols)}}
