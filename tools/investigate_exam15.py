"""Read-only saved-run investigation; synthetic broker probes never call an API."""
import json, hashlib, sys
from pathlib import Path
from datetime import datetime, timedelta
from decimal import Decimal
from collections import Counter
from zoneinfo import ZoneInfo
import duckdb

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from exam_replay.broker import ExamBroker

def sha(p):
    with p.open('rb') as h:return hashlib.file_digest(h,'sha256').hexdigest()
def dt(s):return datetime.fromisoformat(str(s).replace('Z','+00:00'))
def walk(x,path=''):
    if isinstance(x,dict):
        yield path,x
        for k,v in x.items():yield from walk(v,path+'/'+k)
    elif isinstance(x,list):
        for i,v in enumerate(x):yield from walk(v,path+'/'+str(i))

def main():
    folder=ROOT/'data/exam_replay_runs/full15'
    report=json.loads((folder/'report.json').read_text(encoding='utf-8'))
    frozen=json.loads((folder/'freeze.json').read_text(encoding='utf-8'))
    db=duckdb.connect(str(ROOT/'data/exam_v2.duckdb'),read_only=True)
    result={'source_hashes':{p.name:sha(p) for p in folder.glob('*') if p.is_file()},
            'days':len(report['days']),'calls':len(report['calls']), 'inputs':len(report['ai_inputs']),
            'raw_outputs':len(report['ai_raw']),'fills':len(report['fills']),
            'modes':dict(Counter(x['mode'] for x in report['ai_inputs'])),
            'warnings':report['warnings'],
            'freeze_mismatches':[p for p,h in frozen['files'].items() if sha(ROOT/p)!=h],
            'data_hash_checks':{}}
    for name in ['exam_data','exam_v2']:
        m=json.loads((ROOT/f'data/{name}.manifest.json').read_text())
        result['data_hash_checks'][name]=sha(ROOT/f'data/{name}.duckdb')==m['db_sha256']
    future=[]; boundary=[]; end_errors=[]; ages=[]; bar_count=0
    for index,item in enumerate(report['ai_inputs']):
        payload=item['payload'];cut=dt(payload['as_of_utc'])
        for path,obj in walk(payload):
            if 'first_published' in obj:
                ages.append((cut-dt(obj['first_published'])).total_seconds()/86400)
            if 'end_at' not in obj:continue
            try:end=dt(obj['end_at'])
            except (ValueError,TypeError):continue
            if end.tzinfo is None:continue
            bar_count+=1
            if end>cut:future.append({'input':index,'path':path,'end':str(end),'asof':str(cut)})
            if '/quote'==path and end+timedelta(seconds=30)>cut:
                boundary.append({'input':index,'symbol':obj.get('symbol'),'end':str(end),'asof':str(cut)})
            if path.startswith('/history/') and obj.get('end_at')==obj.get('ts'):
                end_errors.append({'input':index,'path':path,'ts':obj['ts']})
    result.update(timestamped_objects_checked=bar_count,future_timestamp_objects=future,
                  quote_buffer_violations=boundary,history_end_timestamp_alias_count=len(end_errors),
                  history_end_examples=end_errors[:3],news_age_days={'min':min(ages),'max':max(ages),'mean':sum(ages)/len(ages)})
    cash=100000.;pos={};differences=[]
    for day in report['daily']:
        for fill in report['fills']:
            if dt(fill['at']).astimezone(ZoneInfo('America/New_York')).date().isoformat()!=day['day']:continue
            sign=1 if fill['action'] in ['BUY','OPEN_LONG','COVER_SHORT'] else -1
            cash-=sign*fill['qty']*fill['price']+fill['fee']
            pos[fill['symbol']]=pos.get(fill['symbol'],0)+sign*fill['qty']
        cash-=day['borrow_cost']
        marks=dict(db.execute("SELECT symbol,close FROM bars WHERE timeframe='1Day' AND feed='iex' AND day=?",[day['day']]).fetchall())
        equity=cash+sum(q*marks[s] for s,q in pos.items())
        differences.append({'day':day['day'],'cash_error':cash-day['cash'],'equity_error':equity-day['equity']})
    result['ledger_reconciliation']=differences
    result['reported_return']=report['metrics']['total_return']
    result['recomputed_return']=report['daily'][-1]['equity']/100000.-1
    result['short_fills']=sum(f['action']=='OPEN_SHORT' for f in report['fills'])
    export=ROOT/'data/exam_review_export'
    manifest=json.loads((export/'manifest.json').read_text(encoding='utf-8'))
    result['export_manifest_mismatches']=[x['path'] for x in manifest['original_files']+manifest['files']
                                          if sha(export/x['path'])!=x['sha256']]
    result['original_copy_mismatches']=[x['path'] for x in manifest['original_files']
                                        if sha(folder/Path(x['path']).name)!=x['sha256']]
    exported=[json.loads(s) for s in (export/'ai_decisions.jsonl').read_text(encoding='utf-8').splitlines()]
    result['export_prompt_matches']=sum(x.get('system_prompt_hash_matches_call') is True for x in exported)
    result['limit_fill_reported_slippage']=sum(f['slippage']*f['qty'] for f in report['fills'] if f['reason']=='LIMIT_FILL')
    result['limit_fill_open_price_difference']=0.0
    for f in report['fills']:
        if f['reason']!='LIMIT_FILL':continue
        source=db.execute("SELECT open FROM bars WHERE symbol=? AND timeframe='1Min' AND feed='iex' AND end_at=?",[f['symbol'],dt(f['at'])]).fetchone()
        if source:result['limit_fill_open_price_difference']+=abs(f['price']-source[0])*f['qty']
    result['qqq_baseline_first_minute']=str(db.execute("SELECT ts FROM bars WHERE symbol='QQQ' AND timeframe='1Min' AND feed='iex' AND day=? ORDER BY ts LIMIT 1",[report['days'][0]]).fetchone()[0])
    # Repro: short cost basis after adding; stop cover locks short proceeds.
    at=dt('2026-10-01T09:31:00-04:00')
    b=ExamBroker(['AMD'],initial_cash=Decimal('10000'),fee_per_share=Decimal('0'),slippage_bps=Decimal('0'))
    for qty,price in [(10,100),(10,120)]:
        o=b.submit(symbol='AMD',action='OPEN_SHORT',qty=qty,limit_price=price,submitted_at=at)
        b._apply_fill(o,qty,Decimal(price),at,reason='AUDIT_SYNTHETIC')
    result['synthetic_short_add']={'expected_average':110,'actual_average':str(b.avg_entry['AMD'])}
    b.set_stop('AMD',stop_price=130,at=at,reason='AUDIT_SYNTHETIC')
    bar={'symbol':'AMD','timeframe':'1Min','feed':'iex','ts':at+timedelta(minutes=1),
         'end_at':at+timedelta(minutes=2),'open':130,'high':131,'low':129,'close':130,'volume':10000}
    b.process_bar(bar)
    result['synthetic_short_stop']={'positions':b.positions,'short_proceeds':{k:str(v) for k,v in b.short_proceeds.items()}}
    # Repro: a 09:40 order is filled using a 09:31 bar.
    b=ExamBroker(['AMD'])
    b.submit(symbol='AMD',action='OPEN_LONG',qty=1,limit_price=150,submitted_at=at+timedelta(minutes=9))
    b.process_bar(bar)
    result['synthetic_fill_before_submission']=len(b.fills)
    # Repro: partially filled stop loses half of the remaining protection.
    b=ExamBroker(['AMD'])
    o=b.submit(symbol='AMD',action='OPEN_LONG',qty=100,limit_price=100,submitted_at=at)
    b._apply_fill(o,100,Decimal(100),at,reason='AUDIT_SYNTHETIC')
    stop=b.set_stop('AMD',stop_price=90,at=at,reason='AUDIT_SYNTHETIC')
    b._apply_fill(stop,40,Decimal(90),at+timedelta(minutes=2),reason='AUDIT_SYNTHETIC')
    result['synthetic_partial_stop']={'held':b.positions['AMD'],'stop_total_qty':stop.qty,
                                      'stop_filled':stop.filled_qty,'stop_remaining':stop.remaining}
    result['source_unchanged']=all(sha(folder/name)==h for name,h in result['source_hashes'].items())
    out=ROOT/'data/exam_investigation';out.mkdir(exist_ok=True)
    (out/'findings.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k not in ['source_hashes','warnings','ledger_reconciliation']},ensure_ascii=False,indent=2))
    print('warning_counts',dict(Counter(x['warning'] for x in result['warnings'])))
    print('max_equity_error',max(abs(x['equity_error']) for x in differences))

if __name__=='__main__':main()
