"""Isolated historical limit-order interface. No Alpaca import or network access."""
from __future__ import annotations

from dataclasses import dataclass,field
from datetime import date,datetime,time,timedelta
from decimal import Decimal,ROUND_FLOOR
from pathlib import Path
from uuid import uuid4

from exam_data.store import ET


def money(value):return Decimal(str(value))


@dataclass
class SimOrder:
    id:str
    symbol:str
    action:str
    qty:int
    limit_price:Decimal
    submitted_at:datetime
    filled_qty:int=0
    status:str="new"


@dataclass
class SimFill:
    order_id:str
    symbol:str
    action:str
    qty:int
    price:Decimal
    fee:Decimal
    at:datetime


@dataclass
class HistoricalSimBroker:
    symbols:set[str]
    initial_cash:Decimal=Decimal("100000")
    fee_per_share:Decimal=Decimal("0.005")
    slippage_bps:Decimal=Decimal("5")
    annual_borrow_rate:Decimal=Decimal("0.08")
    participation:Decimal=Decimal("0.10")
    # A short can open only when an external, dated locate record says True.
    short_locates:dict[tuple[date,str],bool]=field(default_factory=dict)
    cash:Decimal=field(init=False)
    positions:dict[str,int]=field(default_factory=dict)
    short_proceeds:dict[str,Decimal]=field(default_factory=dict)
    orders:dict[str,SimOrder]=field(default_factory=dict)
    fills:list[SimFill]=field(default_factory=list)
    borrow_cost:Decimal=Decimal("0")

    def __post_init__(self):
        self.cash=money(self.initial_cash)
        if self.cash<=0:raise ValueError("Initial cash must be positive")
        if self.participation<=0 or self.participation>1:raise ValueError("Invalid participation limit")

    def _reserved_short_proceeds(self):
        return sum(self.short_proceeds.values(),Decimal("0"))

    def snapshot(self,asof,marks=None):
        if asof.tzinfo is None:raise ValueError("Timezone required")
        marks=marks or {}
        equity=self.cash+sum((money(marks.get(s,0))*q for s,q in self.positions.items()),Decimal("0"))
        return {"source":"ISOLATED_SIMULATOR","asof_utc":asof.isoformat(),
                "cash":float(self.cash),"equity":float(equity),"positions":dict(self.positions),
                "open_orders":[{"id":o.id,"symbol":o.symbol,"action":o.action,"qty":o.qty,
                                "filled_qty":o.filled_qty,"limit_price":str(o.limit_price),"status":o.status}
                               for o in self.orders.values() if o.status in {"new","partially_filled"}],
                "historical_short_locates":{
                    symbol:("VERIFIED_AVAILABLE" if self.short_locates.get((asof.astimezone(ET).date(),symbol)) is True
                            else "UNAVAILABLE_OR_UNKNOWN") for symbol in sorted(self.symbols)}}

    def submit(self,*,symbol,action,qty,limit_price,submitted_at,order_id=None):
        if symbol not in self.symbols:raise ValueError("Outside fixed exam pool")
        if action not in {"OPEN_LONG","CLOSE_LONG","REDUCE","SELL","OPEN_SHORT","COVER_SHORT","BUY"}:
            raise ValueError("Unsupported decision action")
        if isinstance(qty,bool) or not isinstance(qty,int) or qty<=0:raise ValueError("Whole positive shares only")
        price=money(limit_price)
        if price<=0:raise ValueError("Positive limit required")
        if submitted_at.tzinfo is None:raise ValueError("Timezone required")
        local=submitted_at.astimezone(ET)
        if not time(9,30)<=local.time()<=time(9,59,30):
            raise ValueError("New strategy orders only in production opening window")
        held=self.positions.get(symbol,0)
        if action in {"CLOSE_LONG","REDUCE","SELL"} and (held<=0 or qty>held):
            raise ValueError("Cannot oversell or cross zero")
        if action=="COVER_SHORT" and (held>=0 or qty>abs(held)):
            raise ValueError("Cannot overcover or cross zero")
        if action in {"OPEN_LONG","BUY"} and held<0:raise ValueError("Cover short before opening long")
        if action=="OPEN_SHORT":
            if held>0:raise ValueError("Close long before opening short")
            if self.short_locates.get((local.date(),symbol)) is not True:
                raise ValueError("Historical borrow locate unavailable")
        oid=order_id or "exam-"+uuid4().hex
        if oid in self.orders:raise ValueError("Duplicate simulated order id")
        order=SimOrder(oid,symbol,action,qty,price,submitted_at)
        self.orders[oid]=order
        return order

    def process_bar(self,bar,*,max_shares=None):
        """Consume a completed IEX one-minute replay bar; DAY orders may fill after 10:00."""
        if bar["timeframe"]!="1Min" or bar["feed"]!="iex":raise ValueError("Only IEX replay bars")
        at=bar["end_at"]
        if at.tzinfo is None:raise ValueError("Timezone required")
        local=at.astimezone(ET)
        if not time(9,30)<local.time()<=time(16):return []
        available=max(0,int((money(bar["volume"])*self.participation).to_integral_value(rounding=ROUND_FLOOR)))
        if max_shares is not None:available=min(available,max_shares)
        filled=[]
        for order in self.orders.values():
            if not available:break
            if order.symbol!=bar["symbol"] or order.status not in {"new","partially_filled"}:continue
            if order.submitted_at>=bar["ts"] or order.submitted_at.astimezone(ET).date()!=local.date():continue
            remaining=order.qty-order.filled_qty
            action=order.action
            buy=action in {"OPEN_LONG","BUY","COVER_SHORT"}
            limit=order.limit_price
            if buy and money(bar["low"])>limit:continue
            if not buy and money(bar["high"])<limit:continue
            opening=money(bar["open"])
            slip=self.slippage_bps/Decimal("10000")
            price=min(limit,opening*(1+slip)) if buy and opening<=limit else limit if buy else (
                  max(limit,opening*(1-slip)) if opening>=limit else limit)
            quantity=min(remaining,available)
            held=self.positions.get(order.symbol,0)
            if action in {"CLOSE_LONG","REDUCE","SELL"}:quantity=min(quantity,max(0,held))
            if action=="COVER_SHORT":quantity=min(quantity,max(0,-held))
            fee=self.fee_per_share*quantity
            if action in {"OPEN_LONG","BUY"}:
                # Short sale proceeds cannot finance new long exposure.
                free=self.cash-self._reserved_short_proceeds()
                quantity=min(quantity,max(0,int((free/(price+self.fee_per_share)).to_integral_value(rounding=ROUND_FLOOR))))
                fee=self.fee_per_share*quantity
            if action=="OPEN_SHORT":
                # Reg-T style initial margin: own cash supplies at least 50%.
                free=self.cash-self._reserved_short_proceeds()
                quantity=min(quantity,max(0,int((free/(price*Decimal("0.5")+self.fee_per_share)).to_integral_value(rounding=ROUND_FLOOR))))
                fee=self.fee_per_share*quantity
            if not quantity:continue
            signed=quantity if buy else -quantity
            if action=="OPEN_SHORT":
                self.short_proceeds[order.symbol]=self.short_proceeds.get(order.symbol,Decimal("0"))+quantity*price
            elif action=="COVER_SHORT" and held<0:
                prior=self.short_proceeds.get(order.symbol,Decimal("0"))
                self.short_proceeds[order.symbol]=prior*(Decimal(abs(held)-quantity)/Decimal(abs(held)))
            self.cash-=signed*price+fee
            self.positions[order.symbol]=held+signed
            order.filled_qty+=quantity
            order.status="filled" if order.filled_qty==order.qty else "partially_filled"
            fill=SimFill(order.id,order.symbol,action,quantity,price,fee,at)
            self.fills.append(fill);filled.append(fill)
            available-=quantity
        return filled

    def expire_day(self,day):
        for order in self.orders.values():
            if order.submitted_at.astimezone(ET).date()==day and order.status in {"new","partially_filled"}:
                order.status="expired"

    def accrue_borrow(self,day,close_marks):
        cost=sum((abs(q)*money(close_marks[s])*self.annual_borrow_rate/Decimal("365")
                  for s,q in self.positions.items() if q<0),Decimal("0"))
        self.cash-=cost;self.borrow_cost+=cost
        return cost
