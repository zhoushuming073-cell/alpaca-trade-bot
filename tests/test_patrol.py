from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from trading_bot.agent_schemas import PatrolReport, parse_patrol
from trading_bot.engine import Engine
from datetime import datetime as _dt


def test_patrol_schema_is_empty_and_has_no_order_fields():
    raw = '{"schema_version":"1.0","mode":"PATROL","run_id":"p1",'           '"as_of_utc":"2026-11-06T15:00:00Z","signals":[],"notes":""}'
    report = parse_patrol(raw, run_id="p1", symbols=["AAPL"], now=datetime(2026, 11, 6, 15, 0, tzinfo=timezone.utc))
    assert report.signals == []


def test_patrol_rejects_order_fields():
    raw = '{"schema_version":"1.0","mode":"PATROL","run_id":"p1",'           '"as_of_utc":"2026-11-06T15:00:00Z","signals":[],"order_intent":{"qty":1}}'
    with pytest.raises(ValidationError):
        PatrolReport.model_validate_json(raw)


def test_patrol_schedule_is_ten_minutes_and_resets_daily():
    engine = SimpleNamespace(
        multi_agent=SimpleNamespace(enabled=True, patrol=SimpleNamespace(interval_seconds=600)),
        _patrol_day="2026-11-06", _last_patrol=datetime(2026, 11, 6, 15, 0, tzinfo=timezone.utc),
        _patrol_seen=set(), _last_expert=None,
    )
    now = datetime(2026, 11, 6, 15, 10, tzinfo=timezone.utc)
    assert Engine._patrol_due(engine, now) is True
    assert Engine._patrol_due(engine, now - timedelta(seconds=1)) is False
    engine._patrol_seen.add("previous-day-signal")
    Engine._reset_research_day(engine, "2026-11-09")
    assert Engine._patrol_due(engine, now + timedelta(days=3)) is True
    assert not engine._patrol_seen


def test_expert_is_not_started_twice_while_running():
    engine = SimpleNamespace(_expert_running=True, store=SimpleNamespace(event=lambda *a, **k: None))
    assert Engine._run_expert(engine, None, datetime.now(timezone.utc), {}, {}, [], "test") is None


class _PatrolAI:
    def __init__(self, report=None, error=None):
        self.report = report or PatrolReport(run_id="p1", as_of_utc=datetime.now(timezone.utc))
        self.error = error
        self.calls = 0
    def complete(self, mode, payload, parser, **kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        return self.report, {"prompt_tokens": 1, "completion_tokens": 1}


def _patrol_engine(ai):
    store = SimpleNamespace(
        event=lambda kind, payload: events.append((kind, payload)),
        agent_memory=lambda *a, **k: {},
    )
    execution = SimpleNamespace(
    )
    patrol = SimpleNamespace(interval_seconds=600, model="deepseek-flash",
                             max_tokens=1200, timeout_seconds=25)
    return SimpleNamespace(
        ai=ai, store=store, execution=execution, symbols=["AAPL"],
        multi_agent=SimpleNamespace(enabled=True, patrol=patrol),
        _expert_running=False, _last_patrol=None, _patrol_seen=set(),
        _collect_universe=lambda session: ({"AAPL": object()}, [], []),
        _scan_summary=lambda daily: "summary",
        _quote_map=lambda symbols: {},
        _base_payload=lambda session, account: {"session": {}},
        _trim_research=lambda: {}, _trim_news=lambda: {},
    )


events = []


def test_patrol_silent_when_no_signal():
    ai = _PatrolAI(PatrolReport(run_id="p1", as_of_utc=datetime.now(timezone.utc)))
    engine = _patrol_engine(ai)
    signals = Engine._run_patrol(engine, None, datetime.now(timezone.utc), {"positions": []}, {}, [])
    assert signals == []
    assert events[-1][0] == "patrol" and events[-1][1]["signal_count"] == 0


def test_patrol_event_and_duplicate_are_deduplicated():
    report = PatrolReport(run_id="p1", as_of_utc=datetime.now(timezone.utc), signals=[
        {"symbol": "AAPL", "category": "BREAKOUT", "direction": "LONG",
         "severity": "HIGH", "summary": "new high", "evidence_ids": []},
    ])
    engine = _patrol_engine(_PatrolAI(report))
    now = datetime.now(timezone.utc)
    first = Engine._run_patrol(engine, None, now, {"positions": []}, {}, [])
    second = Engine._run_patrol(engine, None, now, {"positions": []}, {}, [])
    assert len(first) == 1
    assert second == []


def test_patrol_model_failure_is_logged_and_silent():
    engine = _patrol_engine(_PatrolAI(error=TimeoutError("patrol timeout")))
    assert Engine._run_patrol(engine, None, datetime.now(timezone.utc), {"positions": []}, {}, []) == []
    assert events[-1][0] == "patrol" and events[-1][1]["status"] == "ERROR"


def test_expert_timeout_releases_running_guard():
    def timeout(*args, **kwargs):
        raise TimeoutError("expert deadline")
    engine = SimpleNamespace(
        _expert_running=False, _last_expert=None,
        _run_active_research=timeout,
        store=SimpleNamespace(event=lambda *a, **k: None),
    )
    with pytest.raises(TimeoutError):
        Engine._run_expert(engine, None, datetime.now(timezone.utc), {}, {}, [], "timeout")
    assert engine._expert_running is False
