"""Historical-only orchestration. Explicit transport; no broker/AI client imports."""
import json,heapq
from datetime import datetime,time,timedelta,timezone
from pathlib import Path
from .broker import Broker
from .contracts import time_state,validate_response,confirmed
from .inputs import packet,messages,digest
from .risk import check_order,validate_profile,describe_profile
from exam_data import ExamStore
from exam_data.store import ET

class ProfitTracker:
    def __init__(self):self.state={};self.reviewed={}
    def update(self,broker,bar,available_at):
        s=bar['symbol'];q=broker.positions.get(s,0)
        if not q:self.state.pop(s,None);self.reviewed.pop(s,None);return
        entry=broker.entry_at[s]
        if bar['ts']<entry:return # Never attribute pre-entry intrabar extreme.
        cost=float(broker.avg[s]);direction=1 if q>0 else -1
        current=(float(bar['close'])/cost-1)*direction
        favorable=(float(bar['high'] if q>0 else bar['low'])/cost-1)*direction
        old=self.state.get(s)
        if old and old['entry_at']!=entry.isoformat():old=None;self.reviewed.pop(s,None)
        # Compare absolute prices, then recompute returns using the current
        # average cost after additions. Old percentage returns cannot be reused.
        best=float(bar['high'] if q>0 else bar['low'])
        if old:best=(max if q>0 else min)(best,old['favorable_price'])
        peak=max(0,(best/cost-1)*direction)
        self.state[s]={'entry_at':entry.isoformat(),'cost':cost,'qty':q,'return_fraction':current,
                       'favorable_price':best,'observed_max_return':peak,'giveback_fraction':peak-current,
                       'max_price_interval_end':(bar['end_at'].isoformat() if old is None or best!=old['favorable_price'] else old['max_price_interval_end']),
                       'known_at':available_at.isoformat(),'profit_review_due':current>=.05 and self.reviewed.get(s,0)<(2 if current>=.10 else 1),
                       'note':'5/10% are review milestones only; minute extreme is not guaranteed fill'}
    def mark_reviewed(self):
        for s,x in self.state.items():self.reviewed[s]=max(self.reviewed.get(s,0),2 if x['return_fraction']>=.10 else 1 if x['return_fraction']>=.05 else 0)

class Engine:
    def __init__(self,symbols,db_path,*,transport,output,model='deepseek-flash',mode='EXPLORATORY',short_locates=None,risk_profile=None):
        self.symbols=list(symbols);self.db_path=db_path;self.transport=transport;self.model=model;self.mode=mode
        self.broker=Broker(symbols,short_locates=short_locates);self.tracker=ProfitTracker()
        self.marks={};self.plans={};self.calls=0;self.max_calls_per_day=8
        self.risk_profile=risk_profile or {'enabled':False};validate_profile(self.risk_profile)
        self.volumes={};self.day_equity=100000.;self.peak_equity=100000.
        # Exclusive creation: a run cannot overwrite any prior AI input/output.
        self.audit=Path(output).open('x',encoding='utf-8');self.day_calls=0
    def close(self):self.audit.close()
    def record(self,kind,**fields):
        self.audit.write(json.dumps({'kind':kind,'recorded_at_utc':datetime.now(timezone.utc).isoformat(),**fields},ensure_ascii=False,default=str)+'\n');self.audit.flush()
    def review(self,day,at,stage):
        state=time_state(at)
        if stage!='PREMARKET' and not state['new_order_allowed']:raise ValueError('No AI after window')
        if self.day_calls>=self.max_calls_per_day:
            self.record('review_deferred',asof=at,reason='daily cost cap');return
        account=self.broker.account(at,self.marks)
        focus=list(set(self.broker.positions)|set(self.plans)) if stage!='PREMARKET' else list(self.broker.positions)
        theses={s:[{'decision_id':oid,'entry_time':o.at.isoformat(),
            'reason':o.decision.reason,'setup':o.decision.setup,'evidence_ids':o.decision.evidence_ids,
            'filled_qty':o.filled} for oid,o in self.broker.orders.items()
            if o.filled and o.at<=at and any(f['order_id']==oid and
                datetime.fromisoformat(f['at'])>=self.broker.entry_at[s] for f in self.broker.fills)
            and o.decision.symbol==s and o.decision.action in {'OPEN_LONG','OPEN_SHORT'}]
            for s in self.broker.positions}
        payload=packet(day,at,self.symbols,account,db_path=self.db_path,mode=self.mode,stage=stage,
                       focus=focus,profit_context=self.tracker.state,
                       position_theses=theses,risk_policy=describe_profile(self.risk_profile))
        exact=messages(payload);call_id=f'{day}:{self.day_calls}'
        config={'model':self.model,'temperature':.2,'max_tokens':7000,'tools':False}
        self.record('input',call_id=call_id,model=self.model,config=config,messages=exact,input_sha256=digest(exact),asof=at)
        self.day_calls+=1;self.calls+=1
        raw=None
        try:
            raw=self.transport(exact,config)
            ids=[e['id'] for row in payload['universe'] for e in row['news']+row['financials']]
            parsed=validate_response(raw,symbols=self.symbols,asof=at,account_id=payload['account_snapshot_id'],evidence_ids=ids,required_symbols=self.broker.positions)
            self.record('output',call_id=call_id,raw=raw,parsed=parsed.model_dump(mode='json'))
            for d in parsed.decisions:
                self.plans[d.symbol]=d
            self.tracker.mark_reviewed()
        except Exception as exc:
            self.record('rejected_output',call_id=call_id,raw=raw,error_type=type(exc).__name__,reason=str(exc))
    def dispatch(self,at):
        if not time_state(at)['new_order_allowed']:return
        for s,d in list(self.plans.items()):
            if d.decision_id in self.broker.orders or d.action in {'WAIT','HOLD'} or at>=d.valid_until:continue
            if s not in self.marks or not confirmed(d,self.marks[s]):continue
            try:
                account=self.broker.account(at,self.marks);self.peak_equity=max(self.peak_equity,account['equity'])
                check_order(d,account,self.marks,self.risk_profile,daily_loss=max(0,1-account['equity']/self.day_equity),
                            drawdown=max(0,1-account['equity']/self.peak_equity),observed_volume=self.volumes.get(s))
                self.broker.submit(d,at,self.marks[s])
            except ValueError as exc:self.record('risk_rejection',id=d.decision_id,asof=at,reason=str(exc));self.plans.pop(s)
    def run_day(self,day):
        from datetime import date
        day=date.fromisoformat(str(day));self.day_calls=0;self.plans={}
        pre=datetime.combine(day,time(9),ET)
        with ExamStore(self.db_path,read_only=True) as store:
            rows=store.db.execute("SELECT symbol,close FROM bars WHERE timeframe='1Day' AND feed='iex' AND available_at<=? QUALIFY row_number() OVER(PARTITION BY symbol ORDER BY ts DESC)=1",[pre]).fetchall()
            self.marks.update(dict(rows))
            rows=store.db.execute("SELECT symbol,timeframe,ts,end_at,available_at,open,high,low,close,volume,feed FROM bars WHERE timeframe='1Min' AND feed='iex' AND day=? AND ts>=? AND end_at<=? ORDER BY ts,symbol",[day,datetime.combine(day,time(9,30),ET),datetime.combine(day,time(16),ET)]).fetchall()
            cols=[x[0] for x in store.db.description];bars=[dict(zip(cols,row)) for row in rows]
        self.day_equity=self.broker.account(pre,self.marks)['equity']
        self.review(day,pre,'PREMARKET')
        queue=[];seq=0
        for b in bars:
            # Matching is separate from information availability; completed bar is
            # never visible to the model at the earlier matching timestamp.
            heapq.heappush(queue,(b['end_at'],0,seq,b));seq+=1
            embargo=timedelta(minutes=15) if self.mode=='STRICT' else timedelta(seconds=30)
            publish=max(b['available_at'],b['end_at']+embargo)
            heapq.heappush(queue,(publish,1,seq,b));seq+=1
        next_review=datetime.combine(day,time(9,31),ET)
        while queue:
            at,kind,_,b=heapq.heappop(queue)
            if kind==0:self.broker.process_bar(b);continue
            self.marks[b['symbol']]=b['close'];self.volumes[b['symbol']]=b['volume'];self.tracker.update(self.broker,b,at)
            # Publish every symbol at this timestamp before taking an account snapshot.
            if queue and queue[0][0]==at:continue
            if time_state(at)['new_order_allowed']:
                due=any(x['profit_review_due'] for x in self.tracker.state.values())
                if at>=next_review or due:
                    self.review(day,at,'PROFIT_REVIEW' if due else 'INTRADAY_REVIEW')
                    next_review=at+timedelta(minutes=5)
                self.dispatch(at)
        end=datetime.combine(day,time(16),ET)
        self.broker.expire(day,end);self.broker.accrue_borrow(day,self.marks)
        self.record('daily_account',day=str(day),price_mark_session_close=end,
                    account=self.broker.account(end+timedelta(minutes=15),self.marks),fills=self.broker.fills,events=self.broker.events)
