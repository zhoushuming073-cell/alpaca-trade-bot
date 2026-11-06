"""Reconstruct old decisions from immutable evidence; no network or AI calls."""
import sys,json,hashlib
from pathlib import Path
from datetime import datetime,time
from collections import Counter
from zoneinfo import ZoneInfo
import duckdb,yaml
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
from exam_v2.snapshot import get_v2_snapshot,ExamMode
ET=ZoneInfo('America/New_York')
def dt(s):return datetime.fromisoformat(str(s).replace('Z','+00:00'))
def main():
    r=json.loads((ROOT/'data/exam_replay_runs/full15/report.json').read_text(encoding='utf-8'))
    out=ROOT/'data/decision_v3_audit';out.mkdir(exist_ok=True)
    d=duckdb.connect(str(ROOT/'data/exam_v2.duckdb'),read_only=True)
    inputs=r['ai_inputs'];raws=r['ai_raw'];trades=r['trades']
    result={'direction':Counter(),'setups':Counter(),'position_cases':{},'data_stages':[],
            'entries':[],'capital':r['daily'],'source_report_sha256':hashlib.sha256((ROOT/'data/exam_replay_runs/full15/report.json').read_bytes()).hexdigest()}
    for i,x in enumerate(inputs):
        raw=json.loads(raws[i]['content'])
        if x['mode']=='UNIVERSE_SCAN':
            for c in raw.get('candidates',[]):
                result['direction'][c['trade_decision']]+=1;result['setups'][c['setup_type']]+=1
    for symbol in ['TSM','MRVL','LITE','MU']:
        t=next(t for t in trades if t['symbol']==symbol)
        rows=d.execute("SELECT ts,end_at,high FROM bars WHERE symbol=? AND timeframe='1Min' AND feed='iex' AND ts>=? AND end_at<=? ORDER BY high DESC,ts LIMIT 1",[symbol,dt(t['entry_at']),dt(t['exit_at'])]).fetchone()
        reviews=[]
        for i,x in enumerate(inputs):
            p=x['payload'];positions=p.get('positions',p.get('account',{}).get('positions',[]))
            if not isinstance(positions,list):continue
            pos=next((p for p in positions if p.get('symbol')==symbol),None)
            if pos and x['mode'] in ['POSITION_REVIEW','INTRADAY_RECHECK']:
                reviews.append({'input_index':i,'mode':x['mode'],'asof':p['as_of_utc'],'position':pos,'raw_response':raws[i]['content']})
        result['position_cases'][symbol]={'trade':t,'max_bar':{'start_et':rows[0].astimezone(ET).isoformat(),'end_et':rows[1].astimezone(ET).isoformat(),'high':rows[2],
            'mfe_pct':(rows[2]/t['entry_price']-1)*100,'inside_local_window':time(9,30)<=rows[0].astimezone(ET).time()<time(10)},'reviews':reviews}
    symbols=yaml.safe_load((ROOT/'config/watchlist.yaml').read_text(encoding='utf-8'))['symbols']
    for day in ['2026-09-29','2026-10-06','2026-10-14']:
        cutoff=dt(day+'T09:00:00-04:00');snap=get_v2_snapshot(day,cutoff,symbols,mode=ExamMode.EXPLORATORY)
        p=next(x['payload'] for x in inputs if x['mode']=='UNIVERSE_SCAN' and x['payload']['exam']['date']==day)
        rows=[e for s in p['universe'] for e in s['news']];facts=[f for s in p['universe'] for f in s['facts']]
        result['data_stages'].append({'day':day,'db_news_total':d.execute("SELECT COUNT(*) FROM evidence WHERE kind='news'").fetchone()[0],
            'future_or_revised_evidence_excluded':d.execute('SELECT COUNT(*) FROM evidence WHERE first_published>? OR updated_at>?',[cutoff,cutoff]).fetchone()[0],
            'snapshot_evidence':dict(Counter(e['kind'] for e in snap['evidence'])),'snapshot_facts':len(snap['fundamentals']),
            'ai_news_kinds':dict(Counter(e['kind'] for e in rows)),'ai_fact_tags':dict(Counter(e['tag'] for e in facts)),
            'ai_evidence_ids':[e['id'] for e in rows]})
    for day in r['decisions']:
        for c in (day.get('plan') or {}).get('candidates',[]):
            intent=c.get('order_intent')
            if intent:
                result['entries'].append({'day':day['day'],'symbol':c['symbol'],'analysis':c['analysis'],
                    'triggers':c['triggers'],'intent':intent,'events':[e for e in day['events'] if e.get('symbol')==c['symbol']],
                    'fills':[f for f in r['fills'] if f['symbol']==c['symbol'] and dt(f['at']).astimezone(ET).date().isoformat()==day['day']]})
    (out/'root_evidence.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    print(json.dumps({'direction':result['direction'],'setups':result['setups'],'mfe':{s:v['max_bar'] for s,v in result['position_cases'].items()},'data_stages':[{k:v for k,v in x.items() if k!='ai_evidence_ids'} for x in result['data_stages']]},ensure_ascii=False,indent=2))
if __name__=='__main__':main()
