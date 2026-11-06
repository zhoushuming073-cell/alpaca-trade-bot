"""Offline V2 isolation tests. No Alpaca or DeepSeek transport is constructed."""
import json
import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

import pytest

from exam_data import ExamStore
from exam_data.store import ET
from exam_v2.ai_ledger import ExamCallLedger, call_historical_ai
from exam_v2.evaluation import evaluate
from exam_v2.prompts import build_exam_prompt
from exam_v2.simulation import HistoricalSimBroker
from exam_v2.snapshot import ExamMode, get_v2_snapshot


DAY=date(2026,9,25)
CUTOFF=datetime.combine(DAY,time(9),ET)
ACCOUNT={"source":"ISOLATED_SIMULATOR","asof_utc":CUTOFF.isoformat(),"cash":100000.0,
         "equity":100000.0,"positions":{},"open_orders":[],"historical_short_locates":"UNKNOWN"}


def _bar(at,price=100):
    return {"t":at.astimezone(timezone.utc).isoformat(),"o":price,"h":price+1,
            "l":price-1,"c":price,"v":100}


@pytest.fixture
def v2db(tmp_path):
    path=tmp_path/"v2.duckdb"
    with ExamStore(path) as store:
        store.db.execute("INSERT INTO symbols VALUES ('AMD','trade','AMD'),('SPCX','trade','SPCX'),('QQQ','market_reference','QQQ')")
        store.meta("version","exam-2026-11-05-v2")
        store.meta("feed","iex")
        store.db.execute("CREATE TABLE security_identity(symbol VARCHAR,entity_id VARCHAR,valid_from DATE,identity_status VARCHAR)")
        store.db.execute("INSERT INTO security_identity VALUES ('AMD','AMD',DATE '2020-01-01','OBSERVED_NOT_VERIFIED'),('SPCX','SPACEX',DATE '2026-06-12','VERIFIED_IPO_DATE'),('QQQ','QQQ',DATE '1999-01-01','OBSERVED_NOT_VERIFIED')")
        store.put_bars([('AMD',_bar(datetime(2026,9,24,tzinfo=timezone.utc))),
                        ('SPCX',_bar(datetime(2026,5,1,tzinfo=timezone.utc))),
                        ('QQQ',_bar(datetime(2026,9,24,tzinfo=timezone.utc)))],"1Day",feed="iex")
        store.put_bars([('AMD',_bar(datetime.combine(DAY,time(8,43),ET))),
                        ('AMD',_bar(datetime.combine(DAY,time(8,58),ET))),
                        ('AMD',_bar(datetime.combine(DAY,time(8,59),ET)))],"1Min",feed="iex")
        store.put_news({"id":1,"created_at":"2026-09-24T12:00:00Z","headline":"Past news",
                        "summary":"Version not archived","symbols":["AMD"]})
        store.put_sec_filing("AMD",2488,"8-K","0000002488-26-000001","x.htm",
                             datetime(2026,9,24,12,tzinfo=timezone.utc))
        store.put_fundamentals([["AMD","us-gaap","Revenue","USD",date(2026,1,1),date(2026,6,30),
                                 "10-Q","old",datetime(2026,8,1,tzinfo=timezone.utc),100.0,"url",
                                 datetime.now(timezone.utc),"unknown","hash"]])
    return path


@pytest.mark.parametrize("mode,minute_count,news_count,fact_count",[
    (ExamMode.STRICT,1,0,0),(ExamMode.EXPLORATORY,2,1,1)])
def test_modes_and_identity(v2db,mode,minute_count,news_count,fact_count):
    packet=get_v2_snapshot(DAY,CUTOFF,["AMD","SPCX"],mode=mode,db_path=v2db)
    assert packet["feed"]=="iex"
    assert sum(b["timeframe"]=="1Min" for b in packet["bars"])==minute_count
    assert not any(b["symbol"]=="SPCX" for b in packet["bars"])
    assert len([e for e in packet["evidence"] if e["kind"]=="news"])==news_count
    assert len(packet["fundamentals"])==fact_count
    assert all("body" not in e for e in packet["evidence"]) if mode is ExamMode.STRICT else True
    assert all(e["historical_status"].startswith("UNKNOWN") for e in packet["evidence"]) if mode is ExamMode.EXPLORATORY else True


@pytest.mark.parametrize("mode",[ExamMode.STRICT,ExamMode.EXPLORATORY])
def test_future_injection_cannot_change_snapshot_or_prompt(v2db,mode):
    before=get_v2_snapshot(DAY,CUTOFF,["AMD"],mode=mode,db_path=v2db)
    prompt_before=build_exam_prompt(DAY,CUTOFF,["AMD"],mode=mode,db_path=v2db,simulated_account=ACCOUNT)
    with ExamStore(v2db) as store:
        store.put_bars([('AMD',_bar(datetime.combine(DAY,time(9,1),ET),999))],"1Min",feed="iex")
        store.put_bars([('AMD',_bar(datetime.combine(DAY,time(0),ET),999))],"1Day",feed="iex")
        store.put_news({"id":999,"created_at":"2026-09-25T13:00:01Z","headline":"Future",
                        "summary":"Future outcome","symbols":["AMD"]})
        store.put_fundamentals([["AMD","audit","FutureReturn","ratio",DAY,DAY,"AUDIT","future",
                                 CUTOFF+timedelta(seconds=1),999.0,"url",datetime.now(timezone.utc),
                                 "future","hash"]])
        store.db.execute("CREATE TABLE future_returns(symbol VARCHAR, return DOUBLE)")
        store.db.execute("INSERT INTO future_returns VALUES ('AMD',999)")
    after=get_v2_snapshot(DAY,CUTOFF,["AMD"],mode=mode,db_path=v2db)
    prompt_after=build_exam_prompt(DAY,CUTOFF,["AMD"],mode=mode,db_path=v2db,simulated_account=ACCOUNT)
    assert before==after
    assert prompt_before.input_sha256==prompt_after.input_sha256
    assert prompt_before.user==prompt_after.user
    assert "Future outcome" not in prompt_after.user


def test_account_and_scan_provenance_rejected(v2db):
    with pytest.raises(ValueError,match="explicit historical simulator"):
        build_exam_prompt(DAY,CUTOFF,["AMD"],db_path=v2db)
    state={"source":"ALPACA_PAPER","asof_utc":CUTOFF.isoformat(),"cash":100,"equity":100,
           "positions":{},"open_orders":[],"historical_short_locates":"UNKNOWN"}
    with pytest.raises(ValueError,match="isolated simulator"):
        build_exam_prompt(DAY,CUTOFF,["AMD"],simulated_account=state,db_path=v2db)
    stale={**ACCOUNT,"asof_utc":(CUTOFF-timedelta(days=1)).isoformat()}
    with pytest.raises(ValueError,match="exact historical cutoff"):
        build_exam_prompt(DAY,CUTOFF,["AMD"],simulated_account=stale,db_path=v2db)
    with pytest.raises(ValueError,match="requires historical scan"):
        build_exam_prompt(DAY,CUTOFF,["AMD"],stage="PREMARKET_PLAN",db_path=v2db,simulated_account=ACCOUNT)


def test_prompt_does_not_read_live_memory_or_use_web(v2db,monkeypatch):
    def forbidden(*_args,**_kwargs):
        raise AssertionError("Live SQLite or HTTP accessed by historical prompt")
    monkeypatch.setattr(sqlite3,"connect",forbidden)
    import requests
    monkeypatch.setattr(requests.sessions.Session,"request",forbidden)
    prompt=build_exam_prompt(DAY,CUTOFF,["AMD"],db_path=v2db,simulated_account=ACCOUNT)
    assert prompt.messages[0]["role"]=="system"
    body=json.loads(prompt.user)
    assert body["exam"]["current_memory_used"] is False
    assert body["exam"]["no_external_research"] is True
    assert "research_summaries" not in body
    assert body["market_reference"]["history"]["symbol"]=="QQQ"
    assert body["market_reference"]["history"]["daily_recent"]


def test_ledger_modes_and_authorization(v2db,tmp_path):
    envelope=build_exam_prompt(DAY,CUTOFF,["AMD"],db_path=v2db,simulated_account=ACCOUNT)
    with pytest.raises(PermissionError):
        call_historical_ai(envelope,lambda *_:"{}",lambda x:x,ledger_directory=tmp_path)
    with pytest.raises(ValueError,match="credentials"):
        call_historical_ai(envelope,lambda *_:"{}",lambda x:x,authorized=True,
                           model_config={"api_key":"secret"},ledger_directory=tmp_path)
    assert not list(tmp_path.glob("*.sqlite"))
    call_id,result=call_historical_ai(envelope,lambda *_:'{"ok":true}',json.loads,
                                      authorized=True,ledger_directory=tmp_path)
    assert result=={"ok":True}
    with ExamCallLedger(ExamMode.STRICT,directory=tmp_path) as ledger:
        row=ledger.db.execute("SELECT system_prompt,user_prompt,raw_output,status FROM calls WHERE call_id=?",[call_id]).fetchone()
        assert row==(envelope.system,envelope.user,'{"ok":true}',"DONE")
    with ExamCallLedger(ExamMode.EXPLORATORY,directory=tmp_path) as ledger:
        assert ledger.db.execute("SELECT COUNT(*) FROM calls").fetchone()[0]==0


def test_simulator_long_short_and_costs():
    broker=HistoricalSimBroker({"AMD"},initial_cash=Decimal("1000"))
    start=datetime.combine(DAY,time(9,30),ET)
    with pytest.raises(ValueError,match="locate"):
        broker.submit(symbol="AMD",action="OPEN_SHORT",qty=1,limit_price=100,submitted_at=start)
    with pytest.raises(ValueError,match="Whole"):
        broker.submit(symbol="AMD",action="BUY",qty=1.5,limit_price=100,submitted_at=start)
    broker.short_locates[(DAY,"AMD")]=True
    order=broker.submit(symbol="AMD",action="OPEN_SHORT",qty=2,limit_price=99,submitted_at=start)
    ts=datetime.combine(DAY,time(9,31),ET)
    replay={"symbol":"AMD","timeframe":"1Min","feed":"iex","ts":ts,"end_at":ts+timedelta(minutes=1),
            "open":100,"high":101,"low":99,"volume":1000}
    fills=broker.process_bar(replay)
    assert len(fills)==1 and order.status=="filled" and broker.positions["AMD"]==-2
    assert broker.accrue_borrow(DAY,{"AMD":100})>0
    with pytest.raises(ValueError,match="New strategy orders"):
        broker.submit(symbol="AMD",action="COVER_SHORT",qty=1,limit_price=101,
                      submitted_at=datetime.combine(DAY,time(10),ET))


def test_simulator_partial_fill_expiry_and_long_reduction():
    broker=HistoricalSimBroker({"AMD"},initial_cash=Decimal("1000"),participation=Decimal("0.10"))
    start=datetime.combine(DAY,time(9,30),ET)
    order=broker.submit(symbol="AMD",action="OPEN_LONG",qty=5,limit_price=101,
                        submitted_at=start,order_id="historical-plan-1")
    with pytest.raises(ValueError,match="Duplicate"):
        broker.submit(symbol="AMD",action="OPEN_LONG",qty=5,limit_price=101,
                      submitted_at=start,order_id="historical-plan-1")
    ts=datetime.combine(DAY,time(9,31),ET)
    replay={"symbol":"AMD","timeframe":"1Min","feed":"iex","ts":ts,
            "end_at":ts+timedelta(minutes=1),"open":100,"high":101,"low":99,"volume":10}
    assert broker.process_bar(replay)[0].qty==1
    assert order.status=="partially_filled"
    broker.expire_day(DAY)
    assert order.status=="expired" and broker.positions["AMD"]==1
    next_day=date(2026,9,28)
    later=broker.submit(symbol="AMD",action="REDUCE",qty=1,limit_price=99,
                        submitted_at=datetime.combine(next_day,time(9,40),ET))
    replay["ts"]=datetime.combine(next_day,time(9,41),ET)
    replay["end_at"]=replay["ts"]+timedelta(minutes=1)
    broker.process_bar(replay)
    assert later.status=="filled" and broker.positions["AMD"]==0


def test_evaluation_metrics_are_pure():
    result=evaluate([100,110,105],[{"direction":"long","pnl":10,"holding_minutes":20},
                                   {"direction":"short","pnl":-5,"holding_minutes":10}],
                    [0,50,25],qqq_prices=[100,102],pool_prices={"AMD":[100,110]})
    assert result["total_return"]==pytest.approx(.05)
    assert result["long"]["pnl"]==10 and result["short"]["pnl"]==-5
    assert result["qqq_buy_hold_return"]==pytest.approx(.02)
