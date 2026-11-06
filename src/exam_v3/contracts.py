"""One authoritative, direction-neutral structured decision and time contract."""
from datetime import datetime,time,timezone
from enum import StrEnum
from decimal import Decimal
from typing import Literal
from pydantic import BaseModel,ConfigDict,Field,model_validator,field_validator
from exam_data.store import ET,CAL

def time_state(at):
    if at.tzinfo is None:raise ValueError('Timezone required')
    local=at.astimezone(ET);schedule=CAL.schedule(start_date=local.date(),end_date=local.date())
    opened=not schedule.empty and time(9,30)<=local.time()<time(10)
    close=datetime.combine(local.date(),time(10),ET)
    return {'decision_time_et':local.isoformat(),'decision_time_utc':at.astimezone(timezone.utc).isoformat(),
            'market_phase':'CLOSED' if schedule.empty else 'PREMARKET' if local.time()<time(9,30) else 'ACTIVE' if opened else 'AFTER_WINDOW',
            'trading_window_open':opened,'new_order_allowed':opened,
            'seconds_until_window_close':max(0,int((close-local).total_seconds())) if not schedule.empty else 0}

class Strict(BaseModel):
    model_config=ConfigDict(extra='forbid',allow_inf_nan=False)

_SETUP_ALIASES={'EVENT_REPICING':'EVENT_REPRICING','EVENT_REPRECILING':'EVENT_REPRICING',
               'EVENT_REPRESENTATIVE':'EVENT_REPRICING','EVENT_REPRICING':'EVENT_REPRICING'}
def _setup(value):
    if isinstance(value,str):return _SETUP_ALIASES.get(value.upper(),value.upper())
    return value

class Confirmation(Strict):
    kind:Literal['NONE','ABOVE','BELOW']='NONE'
    level:Decimal|None=None
    persistence:Literal['LATCHED','WHILE_VALID']='WHILE_VALID'
    @model_validator(mode='after')
    def check(self):
        if self.kind=='NONE' and self.level is not None:raise ValueError('NONE cannot have a level')
        if self.kind!='NONE' and (self.level is None or self.level<=0):raise ValueError('Positive confirmation level required')
        return self

class Decision(Strict):
    schema_version:Literal['3.0']='3.0'
    decision_id:str=Field(min_length=1)
    symbol:str
    action:Literal['OPEN_LONG','OPEN_SHORT','CLOSE_LONG','COVER_SHORT','REDUCE','HOLD','WAIT','PROTECT','REMOVE_PROTECTION']
    setup:Literal['TREND_CONTINUATION','PULLBACK_RECLAIM','FAILED_RALLY','BREAKDOWN','EVENT_REPRICING','POSITION_MANAGEMENT','NONE']
    long_case:str
    short_case:str
    no_trade_case:str
    reason:str=Field(min_length=1)
    qty:int|None=Field(default=None,strict=True,gt=0)
    limit_price:Decimal|None=Field(default=None,gt=0)
    order_type:Literal['limit']='limit'
    confirmation:Confirmation=Field(default_factory=Confirmation)
    valid_until:datetime
    stop_price:Decimal|None=Field(default=None,gt=0)
    take_profit:Decimal|None=Field(default=None,gt=0)
    protection_tif:Literal['day','gtc']='gtc'
    evidence_ids:list[str]=Field(default_factory=list)
    profit_review:str=Field(min_length=1)
    sizing_reason:str=Field(min_length=1)
    @field_validator('setup',mode='before')
    @classmethod
    def _normalize_setup(cls,value):return _setup(value)
    @model_validator(mode='after')
    def check(self):
        if self.valid_until.tzinfo is None:raise ValueError('Timezone required')
        trade=self.action in {'OPEN_LONG','OPEN_SHORT','CLOSE_LONG','COVER_SHORT','REDUCE'}
        if trade and (self.qty is None or self.limit_price is None):raise ValueError('Exact shares and limit required')
        if not trade and (self.qty is not None or self.limit_price is not None):raise ValueError('Nontrade must not contain order')
        if self.action in {'HOLD','WAIT','REMOVE_PROTECTION'} and (self.stop_price or self.take_profit):raise ValueError('Do not silently change protection')
        if self.action=='PROTECT' and self.stop_price is None:raise ValueError('Protection requires stop')
        if self.action=='OPEN_LONG':
            if self.stop_price is not None and self.stop_price>=self.limit_price:raise ValueError('Long stop below entry')
            if self.take_profit is not None and self.take_profit<=self.limit_price:raise ValueError('Long target above entry')
            if self.confirmation.kind=='ABOVE' and self.limit_price<self.confirmation.level:raise ValueError('Buy limit below confirmation')
        if self.action=='OPEN_SHORT':
            if self.stop_price is not None and self.stop_price<=self.limit_price:raise ValueError('Short stop above entry')
            if self.take_profit is not None and self.take_profit>=self.limit_price:raise ValueError('Short target below entry')
            if self.confirmation.kind=='BELOW' and self.limit_price>self.confirmation.level:raise ValueError('Sell limit above confirmation')
        if self.take_profit is not None and self.stop_price is None:raise ValueError('Use linked protection; target alone unsupported')
        return self

def confirmed(decision,price):
    c=decision.confirmation
    return c.kind=='NONE' or (c.kind=='ABOVE' and Decimal(str(price))>=c.level) or (c.kind=='BELOW' and Decimal(str(price))<=c.level)

def validate_response(raw,*,symbols,asof,account_id,evidence_ids,required_symbols=()):
    response=Response.model_validate_json(raw)
    if response.as_of_utc!=asof or response.account_snapshot_id!=account_id:raise ValueError('Historical input binding mismatch')
    seen=set()
    for d in response.decisions:
        if d.symbol not in symbols or d.decision_id in seen:raise ValueError('Unknown symbol or duplicate decision')
        d.evidence_ids=[eid for eid in d.evidence_ids if eid in evidence_ids]
        seen.add(d.decision_id)
    if not set(required_symbols)<={d.symbol for d in response.decisions}:raise ValueError('Held position omitted from review')
    if len({d.symbol for d in response.decisions})!=len(response.decisions):raise ValueError('Conflicting decisions for same symbol')
    return response

class ScanItem(Strict):
    symbol:str
    direction:Literal['LONG','SHORT','NEUTRAL','INSUFFICIENT']
    setup:Literal['TREND_CONTINUATION','PULLBACK_RECLAIM','FAILED_RALLY','BREAKDOWN','EVENT_REPRICING','NONE']
    thesis:str=Field(min_length=1,max_length=1600)
    evidence_ids:list[str]=Field(default_factory=list)
    priority:int=Field(ge=1,le=12)
    @field_validator('setup',mode='before')
    @classmethod
    def _normalize_setup(cls,value):return _setup(value)

class ScanResponse(Strict):
    schema_version:Literal['3.0']='3.0'
    as_of_utc:datetime
    account_snapshot_id:str
    candidates:list[ScanItem]=Field(default_factory=list,max_length=4)
    skipped_symbols:list[str]=Field(default_factory=list,max_length=100)

def validate_scan(raw,*,symbols,asof,account_id,evidence_ids):
    response=ScanResponse.model_validate_json(raw)
    if response.as_of_utc!=asof or response.account_snapshot_id!=account_id:raise ValueError('Scan input binding mismatch')
    seen=set()
    for c in response.candidates:
        if c.symbol not in symbols or c.symbol in seen:raise ValueError('Unknown/duplicate scan symbol')
        c.evidence_ids=[eid for eid in c.evidence_ids if eid in evidence_ids]
        seen.add(c.symbol)
    return response


class Response(Strict):
    schema_version:Literal['3.0']='3.0'
    as_of_utc:datetime
    account_snapshot_id:str
    decisions:list[Decision]
