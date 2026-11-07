"""Prepare or execute at most three authorized historical API validations; no execution."""
import sys,os,json,hashlib,argparse
from pathlib import Path
from datetime import datetime,timezone
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
import duckdb
from exam_v3.inputs import packet,messages,digest
from exam_v3.contracts import validate_response
from exam_v3.risk import describe_profile
import yaml

OUT=ROOT/'data/decision_v3_validation_r4'
def sha(p):
    with p.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def sources():
    paths=list((ROOT/'src/exam_v3').glob('*.py'))+[ROOT/'archive/legacy_prompts/decision_v3.txt',ROOT/'config/aggressive_paper_v3.yaml',Path(__file__),ROOT/'src/exam_v2/snapshot.py',ROOT/'src/exam_data/store.py']+[x for x in [ROOT/'requirements.lock.txt',ROOT/'pyproject.toml',ROOT/'requirements.txt'] if x.exists()]
    return {p.relative_to(ROOT).as_posix():sha(p) for p in paths}
def original_thesis(symbol,entry_time,qty):
    report=json.loads((ROOT/'data/exam_replay_runs/full15/report.json').read_text(encoding='utf-8'))
    for day in report.get('decisions',[]):
        for candidate in (day.get('plan') or {}).get('candidates',[]):
            if candidate.get('symbol')==symbol and candidate.get('trade_decision')=='OPEN_LONG':
                return {symbol:[{
                    'decision_id':candidate.get('plan_id'),
                    'entry_time':entry_time,
                    'reason':candidate.get('analysis',''),
                    'setup':candidate.get('setup_type','UNKNOWN'),
                    'evidence_ids':candidate.get('evidence_ids',[]),
                    'filled_qty':qty,
                    'source':'original_exam15_plan; current position is an isolated single-symbol regression fixture',
                }]}
    return {symbol:[{'decision_id':'UNKNOWN','entry_time':entry_time,
                     'reason':'Original entry reason not found in retained exam JSON',
                     'setup':'UNKNOWN','evidence_ids':[],'filled_qty':qty,
                     'source':'UNKNOWN'}]}

def prepare():
    OUT.mkdir(exist_ok=True)
    if (OUT/'freeze.json').exists():raise RuntimeError('Validation already frozen; do not overwrite')
    samples=[('2026-09-29','09:00:00','AMD',0,0,None),
             ('2026-10-06','09:35:00','MRVL',20,263.941905,'2026-09-30T09:32:00-04:00'),
             ('2026-10-09','09:35:00','LITE',5,977.2934025,'2026-09-30T09:35:00-04:00')]
    db=ROOT/'data/exam_v2.duckdb';prepared=[]
    profile=yaml.safe_load((ROOT/'config/aggressive_paper_v3.yaml').read_text(encoding='utf-8'))
    with duckdb.connect(str(db),read_only=True) as conn:
        for day,clock,symbol,qty,cost,entry in samples:
            at=datetime.fromisoformat(day+'T'+clock+'-04:00')
            mark=conn.execute("SELECT close FROM bars WHERE symbol=? AND feed='iex' AND available_at<=? ORDER BY ts DESC LIMIT 1",[symbol,at]).fetchone()[0]
            account={'source':'ISOLATED_SIMULATOR','asof_utc':at.isoformat(),'cash':100000-qty*cost,'equity':100000+qty*(mark-cost),
                     'positions':{symbol:qty} if qty else {},'open_orders':[],'cost_basis':{symbol:cost} if qty else {},
                     'available_non_margin_cash':100000-qty*cost,'gross_exposure':qty*mark,'historical_short_locates':'ALL_SYMBOLS_ASSUMED_AVAILABLE_FOR_EXAM','shorting_enabled':True,
                     'state_note':'Single-symbol regression fixture using original historical entry cost, not full original portfolio'}
            profit={}
            if qty:
                row=conn.execute("SELECT high,end_at FROM bars WHERE symbol=? AND timeframe='1Min' AND feed='iex' AND ts>=? AND available_at<=? ORDER BY high DESC,ts LIMIT 1",[symbol,entry,at]).fetchone()
                profit[symbol]={'cost':cost,'qty':qty,'current_price':mark,'return_fraction':mark/cost-1,
                                'observed_max_return':row[0]/cost-1,'max_price_interval_end':str(row[1]),
                                'giveback_fraction':(row[0]-mark)/cost,'known_at':at.isoformat(),
                                'existing_stop':251 if symbol=='MRVL' else 921,
                                'note':'Observed historical minute extreme, not executable peak price'}
            theses=original_thesis(symbol,entry,qty) if qty else {}
            p=packet(day,at,[symbol],account,db_path=db,stage='PROFIT_REVIEW' if qty else 'PREMARKET',profit_context=profit,
                     position_theses=theses,risk_policy=describe_profile(profile))
            msg=messages(p);size=len(json.dumps(msg,ensure_ascii=False).encode('utf-8'))
            if size>110000:raise RuntimeError('Validation input byte budget exceeded')
            item={'messages':msg,'payload':p,'input_sha256':digest(msg),'bytes':size,'required_symbols':[symbol] if qty else [],'symbols':[symbol]}
            name=f'{day}_{symbol}.json'
            (OUT/name).write_text(json.dumps(item,ensure_ascii=False,default=str,indent=2),encoding='utf-8')
            prepared.append({'file':name,'sha256':sha(OUT/name),'bytes':size})
    frozen={'version':'decision-v3-2026-11-06-r4','sources':sources(),'dataset_sha256':sha(db),'cases':prepared,
            'model':'deepseek-flash','reported_model_expected':'DeepSeek-V4.1-Flash-1017','temperature':.2,
            'max_tokens':3000,'max_calls':3,'retries':0,'tools':False,
            'estimated_upper_cny_peak':sum(x['bytes']+1000 for x in prepared)*2/1e6+9000*8/1e6,
            'scope':'three regression API decisions only; no broker execution or performance scoring'}
    (OUT/'freeze.json').write_text(json.dumps(frozen,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(frozen,ensure_ascii=False))
def call():
    f=json.loads((OUT/'freeze.json').read_text(encoding='utf-8'))
    if f['sources']!=sources() or sha(ROOT/'data/exam_v2.duckdb')!=f['dataset_sha256']:raise RuntimeError('Frozen source changed')
    # Reuse the already configured DeepSeek key only. No Alpaca keys or client loaded.
    key=os.environ.get('DEEPSEEK_API_KEY')
    if not key:key=(ROOT.parent/'api-key.txt').read_text(encoding='utf-8-sig').strip()
    from openai import OpenAI
    client=OpenAI(api_key=key,base_url='https://api.deepseek.com',timeout=90,max_retries=0)
    results=[]
    with (OUT/'actual_calls.jsonl').open('x',encoding='utf-8') as audit:
        for index,c in enumerate(f['cases']):
            if sha(OUT/c['file'])!=c['sha256']:raise RuntimeError('Frozen input changed')
            item=json.loads((OUT/c['file']).read_text(encoding='utf-8'));p=item['payload'];now=datetime.now(timezone.utc).isoformat()
            audit.write(json.dumps({'kind':'sent','index':index,'at_utc':now,'messages':item['messages'],'config':{k:f[k] for k in ['model','temperature','max_tokens','tools']}},ensure_ascii=False)+'\n');audit.flush()
            record={'index':index,'file':c['file'],'invoked_at_utc':now,'input_sha256':item['input_sha256']}
            try:
                response=client.chat.completions.create(model=f['model'],messages=item['messages'],temperature=f['temperature'],max_tokens=f['max_tokens'],response_format={'type':'json_object'},extra_body={'thinking':{'type':'disabled'}})
                raw=response.choices[0].message.content;record.update(raw=raw,usage=response.usage.model_dump(),provider_id=response.id,provider_model=response.model,finish_reason=response.choices[0].finish_reason)
                if record['finish_reason']!='stop':raise ValueError('Truncated response')
                ids=[e['id'] for r in p['universe'] for e in r['news']+r['financials']]
                parsed=validate_response(raw,symbols=item['symbols'],asof=datetime.fromisoformat(p['as_of_utc']),account_id=p['account_snapshot_id'],evidence_ids=ids,required_symbols=item['required_symbols'])
                record.update(status='VALID',parsed=parsed.model_dump(mode='json'))
            except Exception as exc:record.update(status='FAILED',error_type=type(exc).__name__)
            record['completed_at_utc']=datetime.now(timezone.utc).isoformat()
            audit.write(json.dumps({'kind':'result',**record},ensure_ascii=False)+'\n');audit.flush();results.append(record)
            print(json.dumps({'index':index,'status':record['status'],'actions':[x['action'] for x in record.get('parsed',{}).get('decisions',[])]}),flush=True)
    (OUT/'results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--call',action='store_true')
    parser.add_argument('--output-dir',default=str(OUT));args=parser.parse_args()
    OUT=Path(args.output_dir)
    call() if args.call else prepare()
