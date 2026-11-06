"""Anti-cheat checks use only isolated temporary DuckDB files."""
import hashlib
import json
from datetime import date, datetime, time, timedelta, timezone

import pytest

from exam_data import ExamStore, get_exam_snapshot
from exam_data.store import ET


def bar(timestamp, price=100):
    return {"t":timestamp.astimezone(timezone.utc).isoformat(),"o":price,"h":price+1,
            "l":price-1,"c":price,"v":100}


def sha(packet):
    return hashlib.sha256(json.dumps(packet,sort_keys=True,default=str).encode()).hexdigest()


@pytest.mark.parametrize("exam_day,utc_hour",[
    (date(2026,3,9),13),   # first NYSE session after spring DST switch
    (date(2026,8,27),13),
    (date(2026,11,2),14),  # first NYSE session after autumn DST switch
    (date(2026,11,5),14),
])
def test_0900_future_injection_does_not_change_full_packet(tmp_path,exam_day,utc_hour):
    path=tmp_path/"isolated.duckdb"
    cutoff=datetime.combine(exam_day,time(9),ET)
    assert cutoff.astimezone(timezone.utc).hour==utc_hour
    previous=exam_day-timedelta(days=3 if exam_day.weekday()==0 else 1)
    with ExamStore(path) as store:
        store.db.execute("INSERT INTO symbols VALUES ('AMD','trade','AMD'),('QQQ','market_reference','proxy')")
        store.meta("feed","sip")
        store.put_bars([("AMD",bar(datetime.combine(previous,time(0),ET)))],"1Day",feed="sip")
        store.put_bars([("AMD",bar(datetime.combine(exam_day,time(8,58),ET)))],"1Min",feed="sip")
    before=get_exam_snapshot(exam_day,cutoff,["AMD"],db_path=path)
    with ExamStore(path) as store:
        store.put_bars([("AMD",bar(datetime.combine(exam_day,time(8,59),ET),999)),
                        ("AMD",bar(datetime.combine(exam_day,time(9),ET),999))],"1Min",feed="sip")
        store.put_bars([("AMD",bar(datetime.combine(exam_day,time(0),ET),999))],"1Day",feed="sip")
        after_time=(cutoff+timedelta(seconds=1)).astimezone(timezone.utc).isoformat()
        store.put_news({"id":999,"created_at":after_time,"updated_at":after_time,
                        "headline":"future","summary":"future return","symbols":["AMD"]})
        store.put_fundamentals([["AMD","audit","FutureReturnLabel","ratio",exam_day,exam_day,"AUDIT",
                                 "audit-accession",cutoff+timedelta(seconds=1),123.0,"audit",cutoff,
                                 "injected_future","hash"]])
        store.db.execute("CREATE TABLE future_returns(day DATE,symbol VARCHAR,value DOUBLE)")
        store.db.execute("INSERT INTO future_returns VALUES (?,?,?)",[exam_day,"AMD",123.0])
    after=get_exam_snapshot(exam_day,cutoff,["AMD"],db_path=path)
    assert before==after
    assert sha(before)==sha(after)
    assert all(b["available_at"]<=cutoff for b in after["bars"])


def test_cross_day_minutes_and_revised_news_stay_out(tmp_path):
    path=tmp_path/"isolated.duckdb"
    day=date(2026,9,25)
    previous=date(2026,9,24)
    with ExamStore(path) as store:
        store.db.execute("INSERT INTO symbols VALUES ('AMD','trade','AMD')")
        store.meta("feed","sip")
        store.put_bars([("AMD",bar(datetime.combine(previous,time(15,59),ET)))],"1Min",feed="sip")
        store.put_bars([("AMD",bar(datetime.combine(previous,time(0),ET)))],"1Day",feed="sip")
        store.put_news({"id":1,"created_at":"2026-09-24T19:00:00Z","updated_at":"2026-09-25T14:00:00Z",
                        "headline":"revised later","summary":"current text","symbols":["AMD"]})
    packet=get_exam_snapshot(day,datetime.combine(day,time(9),ET),["AMD"],db_path=path)
    assert [b["timeframe"] for b in packet["bars"]]==["1Day"]
    assert not packet["evidence"]


def test_intraday_review_waits_for_minute_completion_buffer(tmp_path):
    path=tmp_path/"isolated.duckdb"
    day=date(2026,9,25)
    with ExamStore(path) as store:
        store.db.execute("INSERT INTO symbols VALUES ('AMD','trade','AMD')")
        store.meta("feed","sip")
        store.put_bars([("AMD",bar(datetime.combine(day,time(9,34),ET)))],"1Min",feed="sip")
        store.put_bars([("AMD",bar(datetime.combine(day,time(0),ET)))],"1Day",feed="sip")
    early=get_exam_snapshot(day,datetime.combine(day,time(9,35),ET),["AMD"],db_path=path)
    late=get_exam_snapshot(day,datetime.combine(day,time(9,35,30),ET),["AMD"],db_path=path)
    assert not early["bars"]
    assert len(late["bars"])==1 and late["bars"][0]["timeframe"]=="1Min"
