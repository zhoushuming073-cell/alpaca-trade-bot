"""Offline real-input regression and isolated future-injection check."""
import sys,json,shutil,tempfile,hashlib
from pathlib import Path
from datetime import datetime,timedelta
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
import duckdb
from exam_v3.inputs import packet,messages,digest
from exam_v3.engine import Engine
from exam_v3.contracts import Decision
from exam_data import ExamStore
from exam_data.store import ET

def main():
    out=ROOT/'data/decision_v3_audit';out.mkdir(exist_ok=True)
    db=ROOT/'data/exam_v2.duckdb';symbols=['AMD','TSM','MRVL','LITE','MU']
    checks=[];baseline={}
    for day in ['2026-09-29','2026-10-06','2026-10-14']:
        at=datetime.fromisoformat(day+'T09:00:00-04:00')
        account={'source':'ISOLATED_SIMULATOR','asof_utc':at.isoformat(),'cash':100000,'equity':100000,'positions':{},'open_orders':[]}
        p=packet(day,at,symbols,account,db_path=db)
        baseline[day]=digest(messages(p))
        checks.append({'day':day,'counts':{r['symbol']:{'news':sum(e['kind']=='news' for e in r['news']),
            'facts':len(r['financials']),'tags':r['selection_audit']['selected_fact_tags'],
            'latest_news':max((str(e['first_published']) for e in r['news'] if e['kind']=='news'),default=None)} for r in p['universe']},
                       'input_sha256':baseline[day],'input_chars':len(json.dumps(messages(p),default=str))})
        (out/(day+'_v3_input.json')).write_text(json.dumps(messages(p),ensure_ascii=False,indent=2),encoding='utf-8')
    injections=[]
    with tempfile.TemporaryDirectory(prefix='v3-injection-') as temp:
        copy=Path(temp)/'test.duckdb';shutil.copyfile(db,copy)
        for day in baseline:
            at=datetime.fromisoformat(day+'T09:00:00-04:00')
            with duckdb.connect(str(copy)) as conn:
                conn.execute("DELETE FROM meta WHERE key='freeze_version'")
            with ExamStore(copy) as store:
                stamp=at+timedelta(seconds=1)
                store.put_news({'id':'v3-injection','created_at':stamp.isoformat(),'updated_at':stamp.isoformat(),
                                'headline':'FUTURE_SENTINEL','content':'Future outcome','symbols':['AMD']})
                store.put_bar('AMD','1Min',{'t':stamp.isoformat(),'o':999,'h':1000,'l':998,'c':999,'v':100})
            account={'source':'ISOLATED_SIMULATOR','asof_utc':at.isoformat(),'cash':100000,'equity':100000,'positions':{},'open_orders':[]}
            after=packet(day,at,symbols,account,db_path=copy)
            injections.append({'day':day,'unchanged':digest(messages(after))==baseline[day]})
            # Restore the isolated copy between dates; no future fixture survives.
            shutil.copyfile(db,copy)
    # Exercise an actual day with fake responses and an isolated output file.
    def fake(msgs,config):
        p=json.loads(msgs[1]['content'])
        return json.dumps({'schema_version':'3.0','as_of_utc':p['as_of_utc'],
                           'account_snapshot_id':p['account_snapshot_id'],'decisions':[]})
    with tempfile.TemporaryDirectory(prefix='v3-engine-') as temp:
        engine=Engine(['AMD'],db,transport=fake,output=Path(temp)/'run.jsonl')
        engine.run_day('2026-09-29');engine.close()
        records=[json.loads(s) for s in (Path(temp)/'run.jsonl').read_text(encoding='utf-8').splitlines()]
        end=records[-1];engine_result={'calls':engine.calls,'fills':len(engine.broker.fills),'equity':end['account']['equity'],
                                      'errors':[r for r in records if r['kind']=='rejected_output']}
    result={'inputs':checks,'future_injection':injections,'fake_day':engine_result}
    (out/'offline_validation.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))
    assert all(x['unchanged'] for x in injections) and not engine_result['errors']
if __name__=='__main__':main()
