"""Offline counterexamples. Fake model decisions prove execution, not AI skill."""
import json
from datetime import datetime,timedelta,date,time
from decimal import Decimal
import pytest
from exam_data.store import ET
from exam_v3.contracts import Decision,Response,time_state,validate_response
from exam_v3.broker import Broker
from exam_v3.inputs import select_evidence
from exam_v3.engine import ProfitTracker,Engine
from exam_v3.alpaca_contract import prepare_request
from exam_v3.risk import check_order
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

AT=datetime(2026,10,1,9,31,tzinfo=ET)
def decision(action='OPEN_LONG',**kwargs):
    value=dict(decision_id='test',symbol='AMD',action=action,setup='TREND_CONTINUATION',long_case='positive evidence',
               short_case='negative evidence',no_trade_case='no edge',reason='fixture',qty=10,limit_price=100,
               valid_until=AT.replace(hour=10),profit_review='holding remains justified',sizing_reason='explicit budget')
    if action in {'WAIT','HOLD','PROTECT','REMOVE_PROTECTION'}:value.update(qty=None,limit_price=None)
    return Decision(**(value|kwargs))
def bar(at=None,**kw):
    at=at or AT+timedelta(minutes=1)
    return dict(symbol='AMD',timeframe='1Min',feed='iex',ts=at,end_at=at+timedelta(minutes=1),
                open=100,high=101,low=99,close=100,volume=10000)|kw

@pytest.mark.parametrize('hh,mm,ss,allowed',[(9,0,0,False),(9,30,0,True),(9,31,0,True),(9,59,30,True),(9,59,59,True),(10,0,0,False)])
def test_window(hh,mm,ss,allowed):assert time_state(AT.replace(hour=hh,minute=mm,second=ss))['new_order_allowed']==allowed
@pytest.mark.parametrize('day,utc_hour',[(date(2026,3,9),13),(date(2026,11,2),14)])
def test_dst(day,utc_hour):
    at=datetime(day.year,day.month,day.day,9,31,tzinfo=ET)
    state=time_state(at);assert datetime.fromisoformat(state['decision_time_utc']).hour==utc_hour
    assert state['seconds_until_window_close']==1740
def test_holiday():assert not time_state(datetime(2026,7,3,9,31,tzinfo=ET))['new_order_allowed']

@pytest.mark.parametrize('action',['OPEN_LONG','OPEN_SHORT','WAIT','HOLD'])
def test_symmetric_decisions_no_forced_trade(action):
    b=Broker(['AMD'],short_locates={(AT.date(),'AMD'):True});d=decision(action)
    b.submit(d,AT,100);b.process_bar(bar())
    assert b.positions.get('AMD',0)==(10 if action=='OPEN_LONG' else -10 if action=='OPEN_SHORT' else 0)

def test_confirmation_and_gap_do_not_bypass():
    b=Broker(['AMD']);d=decision(limit_price=105,confirmation={'kind':'ABOVE','level':101,'persistence':'WHILE_VALID'})
    with pytest.raises(ValueError,match='Confirmation'):b.submit(d,AT,100)
    b.submit(d,AT,102);b.process_bar(bar(open=98,low=97,high=100))
    assert not b.fills and b.orders['test'].status=='CANCELED'
    with pytest.raises(ValueError,match='below confirmation'):decision(limit_price=99,confirmation={'kind':'ABOVE','level':101})

def test_same_bar_cannot_fill_new_decision():
    b=Broker(['AMD']);b.submit(decision(),AT+timedelta(seconds=30),100)
    b.process_bar(bar(at=AT));assert not b.fills
    b.process_bar(bar(at=AT+timedelta(minutes=1)));assert len(b.fills)==1

def test_partial_protection_shared_quantity_and_gap():
    b=Broker(['AMD']);b.fill('AMD',100,100,AT,'entry','fixture')
    b.protect('AMD',90,120,AT)
    b.process_bar(bar(open=85,low=80,high=121,close=85,volume=400))
    assert b.positions['AMD']==60 and b.groups['AMD']['triggered']=='STOP'
    assert b.fills[-1]['reason']=='LINKED_STOP' and Decimal(b.fills[-1]['price'])<85
    b.process_bar(bar(at=AT+timedelta(minutes=2),open=95,high=96,low=94,volume=600))
    assert not b.positions and not b.groups
    assert sum(f['qty'] for f in b.fills if f['reason']=='LINKED_STOP')==100

def test_short_average_cover_release():
    b=Broker(['AMD']);b.fill('AMD',-10,100,AT,'a','fixture');b.fill('AMD',-10,120,AT,'b','fixture')
    assert b.avg['AMD']==110
    b.protect('AMD',130,90,AT);b.process_bar(bar(open=140,high=142,low=139))
    assert not b.positions and not b.proceeds and not b.groups

def test_profit_review_allows_hold_and_tracks_giveback():
    b=Broker(['AMD']);b.fill('AMD',10,100,AT,'e','fixture');t=ProfitTracker()
    t.update(b,bar(high=108,close=106),AT+timedelta(minutes=3))
    assert t.state['AMD']['profit_review_due'];t.mark_reviewed()
    b.submit(decision('HOLD',reason='Window closed, but this text is not authority'),AT,106)
    assert b.positions['AMD']==10
    t.update(b,bar(at=AT+timedelta(minutes=2),high=112,close=110),AT+timedelta(minutes=4))
    assert t.state['AMD']['profit_review_due']
    assert t.state['AMD']['observed_max_return']==pytest.approx(.12)
    assert t.state['AMD']['giveback_fraction']==pytest.approx(.02)

def test_native_linked_request_not_two_exit_orders():
    d=decision(stop_price=95,take_profit=110,protection_tif='day')
    p=prepare_request(d,at=AT,last_price=100)
    assert p['order_class']=='bracket' and 'take_profit' in p and 'stop_loss' in p
    with pytest.raises(ValueError,match='reconcile'):prepare_request(d,at=AT,last_price=100,has_active_exit=True)
    with pytest.raises(ValueError,match='GTC'):prepare_request(d.model_copy(update={'protection_tif':'gtc'}),at=AT,last_price=100)

def test_newest_news_and_financial_diversity():
    def news(i,kind,age):return dict(id=i,kind=kind,symbols=['AMD'],first_published=AT-timedelta(days=age),title=i,body=i,source='fixture',url='fixture',historical_status='UNKNOWN')
    facts=[dict(symbol='AMD',tag=tag,unit='USD',period_start=date(2026,1,1),period_end=date(2026,6,30),available_at=AT-timedelta(days=1),value=1,historical_status='UNKNOWN') for tag in ['Assets','Revenue','NetIncomeLoss','OperatingIncomeLoss']]
    snapshot={'asof_utc':AT.isoformat(),'mode':'EXPLORATORY','evidence':[news('old','sec_filing',200),news('new','news',1),news('yesterday','news',2)],'fundamentals':facts}
    picked=select_evidence(snapshot,'AMD')
    assert picked['news'][0]['id']=='new' and picked['financials'][0]['tag']=='Revenue'
    snapshot['evidence'].append(news('future','news',-1))
    with pytest.raises(ValueError,match='Future'):select_evidence(snapshot,'AMD')

def test_duplicate_oversell_and_capital_rejected():
    b=Broker(['AMD'],cash=100)
    with pytest.raises(ValueError,match='capital'):b.submit(decision(),AT,100)
    b=Broker(['AMD']);b.submit(decision(),AT,100)
    with pytest.raises(ValueError,match='duplicate'):b.submit(decision(),AT,100)
    b.process_bar(bar())
    with pytest.raises(ValueError,match='cross zero'):b.submit(decision('REDUCE',decision_id='reduce',qty=11),AT,100)

def test_overnight_protection_and_close_after_window():
    b=Broker(['AMD']);b.fill('AMD',10,100,AT,'e','fixture');b.protect('AMD',90,110,AT)
    b.expire(AT.date(),AT.replace(hour=16));assert 'AMD' in b.groups
    b.process_bar(bar(at=AT+timedelta(days=1),open=115,high=116,low=114))
    assert not b.positions and b.fills[-1]['reason']=='LINKED_TARGET'
    with pytest.raises(ValueError,match='Window'):b.submit(decision(),AT.replace(hour=10),100)

def test_response_binds_history_and_rejects_unknown_evidence():
    obj={'as_of_utc':AT.isoformat(),'account_snapshot_id':'a','decisions':[decision('WAIT').model_dump(mode='json')]}
    assert validate_response(json.dumps(obj),symbols=['AMD'],asof=AT,account_id='a',evidence_ids=[])
    obj['decisions'][0]['evidence_ids']=['future']
    cleaned=validate_response(json.dumps(obj),symbols=['AMD'],asof=AT,account_id='a',evidence_ids=[])
    assert cleaned.decisions[0].evidence_ids==[]

def test_unapproved_profile_fails_closed():
    with pytest.raises(ValueError,match='approved'):check_order(decision(),{}, {},{'enabled':True,'approved':False})

def test_risk_caps_block_increases_allow_reduction():
    profile={'enabled':True,'approved':True,'allow_leverage':False,'allow_shorting':True,'limits':{
        'max_single_trade_loss_fraction':.01,'max_symbol_exposure_fraction':.25,'max_gross_exposure_fraction':1,
        'max_daily_loss_fraction':.02,'max_drawdown_fraction':.1,'max_volume_participation':.1}}
    account={'equity':10000,'positions':{},'gross_exposure':0,'open_orders':[]}
    with pytest.raises(ValueError,match='loss budget'):check_order(decision(qty=100,stop_price=90),account,{},profile,observed_volume=10000)
    with pytest.raises(ValueError,match='halt'):check_order(decision(stop_price=99),account,{},profile,daily_loss=.03,observed_volume=10000)
    check_order(decision('REDUCE'),account,{},profile,daily_loss=.03)

def test_partial_close_keeps_remainder_protected():
    b=Broker(['AMD']);b.fill('AMD',100,100,AT,'entry','fixture');b.protect('AMD',90,120,AT)
    b.submit(decision('REDUCE',qty=40,limit_price=105),AT,100)
    b.process_bar(bar(open=85,low=80,high=86))
    assert b.positions['AMD']==40 # 60 unreserved shares stopped; sell limit40 remains
    assert sum(f['qty'] for f in b.fills if f['reason']=='LINKED_STOP')==60

def test_borrow_weekend_not_charged_to_new_monday_position():
    friday=date(2026,10,2);monday=date(2026,10,5)
    b=Broker(['AMD']);assert b.accrue_borrow(friday,{})==0
    b.fill('AMD',-10,100,AT,'entry','fixture')
    assert b.accrue_borrow(monday,{'AMD':100})==Decimal(1000)*Decimal('.08')/365
    with pytest.raises(ValueError,match='Duplicate'):b.accrue_borrow(monday,{'AMD':100})

def test_strict_evidence_never_carries_unknown_bodies():
    s={'asof_utc':AT.isoformat(),'mode':'STRICT','evidence':[{'id':'sec1','kind':'sec_filing_metadata','symbols':['AMD'],
       'first_published':AT-timedelta(days=1),'body':'SHOULD_NOT_BE_SENT','historical_status':'ACCEPTANCE_TIME_ONLY'}],'fundamentals':[]}
    assert 'SHOULD_NOT_BE_SENT' not in json.dumps(select_evidence(s,'AMD'),default=str)

def test_unknown_short_permission_rejected():
    with pytest.raises(ValueError,match='locate'):Broker(['AMD']).submit(decision('OPEN_SHORT'),AT,100)

def test_held_position_must_be_reviewed():
    raw=json.dumps({'as_of_utc':AT.isoformat(),'account_snapshot_id':'a','decisions':[]})
    with pytest.raises(ValueError,match='omitted'):validate_response(raw,symbols=['AMD'],asof=AT,account_id='a',evidence_ids=[],required_symbols=['AMD'])

def test_profit_peak_rebased_after_addition():
    b=Broker(['AMD']);b.fill('AMD',10,100,AT,'e','fixture');t=ProfitTracker()
    t.update(b,bar(high=120,close=110),AT+timedelta(minutes=3))
    b.fill('AMD',10,120,AT+timedelta(minutes=3),'add','fixture')
    t.update(b,bar(at=AT+timedelta(minutes=4),high=119,close=115),AT+timedelta(minutes=6))
    assert t.state['AMD']['observed_max_return']==pytest.approx(120/110-1)

def test_market_order_rejected():
    with pytest.raises(ValueError):decision(order_type='market')

def test_authoritative_window_ignores_wrong_ai_narrative():
    b=Broker(['AMD']);b.submit(decision(reason='The window is already closed'),AT,100)
    b.process_bar(bar());assert b.positions['AMD']==10


def test_packet_exposes_original_thesis_and_active_risk_policy(tmp_path):
    from exam_v3.inputs import packet
    from exam_v3.risk import describe_profile
    account={'source':'ISOLATED_SIMULATOR','asof_utc':AT.isoformat(),'cash':100000,'equity':100000,'positions':{},'open_orders':[]}
    thesis={'AMD':[{'decision_id':'entry-1','entry_time':AT.isoformat(),'reason':'original long thesis','setup':'TREND_CONTINUATION','evidence_ids':[],'filled_qty':10}]}
    policy=describe_profile({'enabled':False})
    p=packet('2026-10-01',AT,['AMD'],account,db_path=ROOT/'data/exam_v2.duckdb',
             position_theses=thesis,risk_policy=policy)
    assert p['position_theses']['AMD'][0]['reason']=='original long thesis'
    assert p['risk_policy']['status']=='DEFAULT_PAPER'
    assert p['risk_policy']['limits'] is None
    assert p['time_state']['new_order_allowed'] is True


def test_protect_must_be_single_decision_without_qty():
    from exam_v3.contracts import Response, validate_response
    base={'decision_id':'protect','symbol':'AMD','action':'PROTECT','setup':'POSITION_MANAGEMENT',
          'long_case':'hold','short_case':'risk','no_trade_case':'none','reason':'tighten stop',
          'stop_price':95,'valid_until':AT.replace(hour=10),'profit_review':'reviewed',
          'sizing_reason':'existing size'}
    with pytest.raises(ValueError,match='Nontrade'):
        Decision(**base,qty=10)
    hold=decision('HOLD',decision_id='hold')
    protect=Decision(**base)
    raw=json.dumps({'as_of_utc':AT.isoformat(),'account_snapshot_id':'a',
                    'decisions':[hold.model_dump(mode='json'),protect.model_dump(mode='json')]})
    with pytest.raises(ValueError,match='Conflicting'):
        validate_response(raw,symbols=['AMD'],asof=AT,account_id='a',evidence_ids=[],required_symbols=['AMD'])


def test_recursive_integrity_accepts_frozen_input_and_rejects_future():
    from exam_v3.integrity import audit_payload
    from exam_v3.inputs import packet
    acct={'source':'ISOLATED_SIMULATOR','asof_utc':AT.isoformat(),'cash':100000,'equity':100000,'positions':{},'open_orders':[]}
    p=packet('2026-10-01',AT,['AMD'],acct,db_path=ROOT/'data/exam_v2.duckdb')
    assert audit_payload(p)['ok']
    p2=json.loads(json.dumps(p,default=str))
    p2['universe'][0]['daily'].append(['2026-10-01T09:31:00-04:00','2026-10-01T09:32:00-04:00','2026-10-01T09:32:30-04:00',1,1,1,1,1])
    result=audit_payload(p2)
    assert not result['ok'] and any('after cutoff' in e['reason'] for e in result['errors'])


def test_qqq_benchmark_uses_regular_session_not_premarket():
    import duckdb
    from exam_v3.evaluation import qqq_session_prices,exposure_matched
    conn=duckdb.connect(str(ROOT/'data/exam_v2.duckdb'),read_only=True)
    p=qqq_session_prices(conn,date(2026,9,28))
    assert p['first_ts'].astimezone(ET).time()==time(9,30)
    assert exposure_matched([(AT,100000,10000),(AT+timedelta(minutes=1),100000,20000)],
                            [(AT,100),(AT+timedelta(minutes=1),110)])==pytest.approx(.01)
    conn.close()


def test_fill_cost_components_are_classified_and_equity_consistent():
    b=Broker(['AMD']);b.fill('AMD',10,Decimal('99'),AT,'l','LIMIT_FILL',limit_price=100,open_price=98)
    f=b.fills[-1]
    assert Decimal(f['price_vs_limit_per_share'])==Decimal('-1')
    assert Decimal(f['slippage_vs_open_per_share'])==Decimal('1')
    b.fill('AMD',-10,Decimal('85'),AT,'s','LINKED_STOP',trigger_price=90,open_price=84)
    f=b.fills[-1]
    assert Decimal(f['gap_cost'])==Decimal('60')  # 6 adverse gap * 10
    assert b.cash==b.initial_cash-10*99+10*85- b.fees


def test_prompt_contains_historical_guard_and_no_tools():
    from exam_v3.inputs import messages,packet
    acct={'source':'ISOLATED_SIMULATOR','asof_utc':AT.isoformat(),'cash':100000,'equity':100000,'positions':{},'open_orders':[]}
    msg=messages(packet('2026-10-01',AT,['AMD'],acct,db_path=ROOT/'data/exam_v2.duckdb'))
    system=msg[0]['content']
    assert 'No web' in system and 'remembered future outcomes' in system and 'No real broker tools' in system


def test_source_rows_match_duckdb_and_no_self_declaration():
    import duckdb
    from exam_v3.integrity import audit_source
    from exam_v3.inputs import packet
    acct={'source':'ISOLATED_SIMULATOR','asof_utc':AT.isoformat(),'cash':100000,'equity':100000,'positions':{},'open_orders':[]}
    p=packet('2026-10-01',AT,['AMD'],acct,db_path=ROOT/'data/exam_v2.duckdb')
    conn=duckdb.connect(str(ROOT/'data/exam_v2.duckdb'),read_only=True)
    result=audit_source(p,conn);conn.close()
    assert result['ok'] and result['checked']>100
