"""Historical evidence selection and exact versioned messages; no external sources."""
import json,hashlib
from pathlib import Path
from datetime import datetime
from collections import Counter
from exam_v2.snapshot import get_v2_snapshot,ExamMode
from .contracts import Response,time_state
from . import VERSION
ROOT=Path(__file__).resolve().parents[2]
def canon(v):return json.dumps(v,sort_keys=True,ensure_ascii=False,default=str,separators=(',',':'))
def digest(v):return hashlib.sha256(canon(v).encode()).hexdigest()
BAR_COLUMNS=['start','end_at','available_at','open','high','low','close','volume']
def packed(bars):
    return [[str(b.get('ts',b.get('period'))),str(b['end_at']),str(b['available_at']),
             b['open'],b['high'],b['low'],b['close'],b['volume']] for b in bars]

def select_evidence(snapshot,symbol,news_k=3,facts_k=12):
    cutoff=datetime.fromisoformat(snapshot['asof_utc'])
    eligible=[e for e in snapshot['evidence'] if not e.get('symbols') or symbol in e['symbols']]
    # Different buckets prevent SEC metadata from crowding out actual news.
    selected=[]
    for kinds,k in [({'news'},news_k),({'sec_filing','sec_filing_metadata'},2)]:
        bucket=sorted((e for e in eligible if e['kind'] in kinds),key=lambda e:(e['first_published'],e['id']),reverse=True)
        for e in bucket[:k]:
            if e['first_published']>cutoff or e.get('updated_at',e['first_published'])>cutoff:raise ValueError('Future evidence')
            selected.append({key:e[key] for key in ['id','kind','source','url','title','first_published','historical_status'] if key in e}|
                            ({'body_excerpt':e.get('body','')[:1400]} if snapshot['mode']=='EXPLORATORY' else {}))
    facts=[f for f in snapshot['fundamentals'] if f['symbol']==symbol]
    priority=['Revenue','Sales','NetIncome','OperatingIncome','EarningsPerShare','OperatingActivities','GrossProfit','Assets','Debt','Cash']
    def rank(f):return next((i for i,x in enumerate(priority) if x.lower() in f['tag'].lower()),len(priority))
    # Latest period and latest visible accession, with tag/unit/period deduplication.
    facts=sorted(facts,key=lambda f:(f['period_end'],f['available_at']),reverse=True)
    tags={};chosen=[];seen=set()
    for f in sorted(facts,key=rank):
        key=(f['tag'],f['unit'],str(f.get('period_start')),str(f['period_end']))
        if key in seen or tags.get(f['tag'],0)>=2:continue
        seen.add(key);tags[f['tag']]=tags.get(f['tag'],0)+1
        if f['available_at']>cutoff:raise ValueError('Future fact')
        chosen.append({'id':'fact:'+digest(f)[:24],**f})
        if len(chosen)>=facts_k:break
    return {'news':selected,'financials':chosen,'selection_audit':{
        'eligible_evidence':dict(Counter(e['kind'] for e in eligible)),
        'selected_evidence':dict(Counter(e['kind'] for e in selected)),
        'eligible_facts':len(facts),'selected_fact_tags':[f['tag'] for f in chosen],
        'truncation':'news body excerpt max1400 chars; separate news/SEC quotas; up to2 periods per financial tag'}}

def packet(day,asof,symbols,account,*,mode=ExamMode.EXPLORATORY,db_path=None,stage='PLAN',focus=None,profit_context=None,position_theses=None,risk_policy=None):
    if account.get('source')!='ISOLATED_SIMULATOR' or datetime.fromisoformat(account['asof_utc'])!=asof:
        raise ValueError('Explicit simulator state at the exact cutoff required')
    snapshot=get_v2_snapshot(day,asof,symbols,mode=mode,db_path=db_path)
    focus=set(symbols if focus is None else focus);rows=[]
    for s in list(symbols)+snapshot['market_reference']:
        daily=[b for b in snapshot['bars'] if b['symbol']==s and b['timeframe']=='1Day'][-130:]
        row={'symbol':s,'tradable':s in symbols,'daily':daily if s in focus else daily[-5:],
             'weekly':[b for b in snapshot['weekly_bars'] if b['symbol']==s][-26:],
             'monthly':[b for b in snapshot['monthly_bars'] if b['symbol']==s][-6:],
             'minutes':[b for b in snapshot['bars'] if b['symbol']==s and b['timeframe']=='1Min'][-20:]}
        row.update(select_evidence(snapshot,s))
        quote_bars=[b for b in snapshot['bars'] if b['symbol']==s and b['timeframe']=='1Min' and b['available_at']<=datetime.fromisoformat(snapshot['asof_utc'])]
        if quote_bars:
            q=quote_bars[-1]
            row['quote']={'symbol':s,'ts':q['ts'].isoformat(),'end_at':q['end_at'].isoformat(),
                          'available_at':q['available_at'].isoformat(),'last':q['close'],
                          'open':q['open'],'high':q['high'],'low':q['low'],'close':q['close'],
                          'volume':q['volume'],'bid':None,'ask':None,'spread':None}
        else:
            row['quote']=None
        rows.append(row)
        row['bar_columns']=BAR_COLUMNS
        row['bar_source']={'feed':'iex','adjustment':'raw','historical_version':'UNKNOWN_PROVIDER_RECONSTRUCTION'}
        for key in ['daily','weekly','monthly','minutes']:row[key]=packed(row[key])
    payload={'version':VERSION,'stage':stage,'as_of_utc':asof.isoformat(),'time_state':time_state(asof),
             'account_snapshot_id':digest(account),'account':account,'universe':rows,
             'profit_context':profit_context or {},'position_theses':position_theses or {},'risk_policy':risk_policy or {'enabled':False,'status':'DEFAULT_PAPER','limits':None},
             'historical_limitations':snapshot['limitations'],
             'execution_contract':{'environment':'ISOLATED_SIMULATOR','whole_shares':True,'order_type':'limit',
                 'entry_tif':'day','protection':['NONE','STOP','LINKED_STOP_TAKE_PROFIT'],
                 'protective_tif':['day','gtc'],'trailing_stop_supported':False,
                 'after_window':'existing orders only','short_locates':account.get('historical_short_locates','UNKNOWN'),
                 'shorting_enabled':bool(account.get('shorting_enabled',False)),
                 'allow_shorting':bool(account.get('shorting_enabled',False) and account.get('historical_short_locates')!='UNAVAILABLE')},
             'response_schema':Response.model_json_schema()}
    return payload

def messages(payload):
    return [{'role':'system','content':(ROOT/'prompts/decision_v3.txt').read_text(encoding='utf-8')},
            {'role':'user','content':canon(payload)}]
