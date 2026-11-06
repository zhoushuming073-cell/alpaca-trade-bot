from datetime import datetime, timezone

import pytest

from exam_data.store import ExamStore, get_exam_snapshot, sessions


def test_sessions_use_completed_nyse_days_and_dst():
    days = sessions(datetime(2026, 11, 6, 1, tzinfo=timezone.utc))
    assert len(days) == 60
    assert days[-1][0].isoformat() == "2026-11-05"
    assert days[-1][1].hour == 14  # US winter open in UTC
    assert days[0][1].hour == 13  # US summer open in UTC
    assert not any(day.weekday() >= 5 for day, *_ in days)


def test_snapshot_excludes_unfinished_bar_and_future_news(tmp_path):
    path = tmp_path / "exam.duckdb"
    with ExamStore(path) as store:
        store.db.execute("INSERT INTO symbols VALUES ('AMD','trade','AMD'),('QQQ','market_reference','Nasdaq-100 proxy')")
        bar = lambda ts: {"t":ts,"o":100,"h":101,"l":99,"c":100,"v":1000}
        store.put_bars([("AMD",bar("2026-11-05T13:58:00Z")),
                        ("AMD",bar("2026-11-05T13:59:00Z")),
                        ("AMD",bar("2026-11-05T14:00:00Z")),
                        ("AMD",bar("2026-11-05T14:30:00Z")),
                        ("QQQ",bar("2026-11-05T13:58:00Z"))], "1Min")
        store.put_news({"id":1,"created_at":"2026-11-05T13:55:00Z","updated_at":"2026-11-05T13:55:00Z",
                        "headline":"early","summary":"known","symbols":["AMD"]})
        store.put_news({"id":2,"created_at":"2026-11-05T14:05:00Z","updated_at":"2026-11-05T14:05:00Z",
                        "headline":"late","summary":"future","symbols":["AMD"]})
        store.put_news({"id":3,"created_at":"2026-11-05T13:50:00Z","updated_at":"2026-11-05T14:05:00Z",
                        "headline":"revised","summary":"current content revised later","symbols":["AMD"]})
    pack = get_exam_snapshot("2026-11-05","2026-11-05T09:00:00-05:00",["AMD"],db_path=path)
    assert {r["ts"].minute for r in pack["bars"] if r["symbol"]=="AMD"} == {58}
    assert [n["title"] for n in pack["evidence"]] == ["early"]
    assert pack["market_reference"] == ["QQQ"]
    assert all(r["available_at"] <= datetime.fromisoformat(pack["asof"]) for r in pack["bars"])


def test_snapshot_rejects_pool_escape_and_naive_time(tmp_path):
    path = tmp_path / "exam.duckdb"
    with ExamStore(path) as store:
        store.db.execute("INSERT INTO symbols VALUES ('AMD','trade','AMD'),('QQQ','market_reference','proxy')")
    with pytest.raises(ValueError):
        get_exam_snapshot("2026-11-05","2026-11-05T09:00:00",["AMD"],db_path=path)
    with pytest.raises(ValueError):
        get_exam_snapshot("2026-11-05","2026-11-05T09:00:00-05:00",["QQQ"],db_path=path)


def test_snapshot_keeps_prior_daily_history_but_not_prior_minutes_or_future_week_close(tmp_path):
    path=tmp_path/"exam.duckdb"
    with ExamStore(path) as store:
        store.db.execute("INSERT INTO symbols VALUES ('AMD','trade','AMD')")
        store.meta("feed","sip")
        day_bar=lambda ts,price: {"t":ts,"o":price,"h":price+1,"l":price-1,"c":price,"v":100}
        store.put_bars([("AMD",day_bar("2026-11-03T05:00:00Z",100)),
                        ("AMD",day_bar("2026-11-04T05:00:00Z",110)),
                        ("AMD",day_bar("2026-11-05T05:00:00Z",999))],"1Day",feed="sip")
        store.put_bars([("AMD",day_bar("2026-11-04T13:00:00Z",100)),
                        ("AMD",day_bar("2026-11-05T13:00:00Z",110))],"1Min",feed="sip")
    pack=get_exam_snapshot("2026-11-05","2026-11-05T09:00:00-05:00",["AMD"],db_path=path)
    daily=[b for b in pack["bars"] if b["timeframe"]=="1Day"]
    minutes=[b for b in pack["bars"] if b["timeframe"]=="1Min"]
    assert [b["close"] for b in daily]==[100,110]
    assert len(minutes)==1 and minutes[0]["ts"].date().isoformat()=="2026-11-05"
    assert len(pack["weekly_bars"])==1 and pack["weekly_bars"][0]["close"]==110


def test_sec_fact_and_filing_respect_acceptance_time(tmp_path):
    path=tmp_path/"exam.duckdb"
    with ExamStore(path) as store:
        store.db.execute("INSERT INTO symbols VALUES ('AMD','trade','AMD')")
        accepted=datetime(2026,11,5,14,30,tzinfo=timezone.utc)
        store.put_sec_filing("AMD",2488,"8-K","0000002488-26-000001","report.htm",accepted)
        store.put_fundamentals([["AMD","us-gaap","Assets","USD",accepted.date(),accepted.date(),"8-K",
                                 "0000002488-26-000001",accepted,1.0,"https://www.sec.gov/",accepted,
                                 "sec_acceptance_timestamp_verified_content_reconstructed","hash"]])
    early=get_exam_snapshot("2026-11-05","2026-11-05T09:00:00-05:00",["AMD"],db_path=path)
    later=get_exam_snapshot("2026-11-05","2026-11-05T10:00:00-05:00",["AMD"],db_path=path)
    assert not early["evidence"] and not early["fundamentals"]
    assert len(later["evidence"])==len(later["fundamentals"])==1


def test_freeze_blocks_writer_before_schema_mutation(tmp_path):
    path=tmp_path/"exam.duckdb"
    with ExamStore(path) as store:
        store.meta("freeze_version","v1")
    before=path.read_bytes()
    with pytest.raises(RuntimeError,match="frozen"):
        ExamStore(path)
    assert path.read_bytes()==before
    with ExamStore(path,read_only=True) as store:
        assert store.scalar("SELECT value FROM meta WHERE key='freeze_version'")=="v1"
