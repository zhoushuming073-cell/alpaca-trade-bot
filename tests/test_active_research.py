from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from test_session_engine import make_engine


@pytest.fixture
def research_engine(store, clock, bundle, monkeypatch):
    config = replace(bundle, multi_agent=bundle.multi_agent.model_copy(update={"enabled": True}))
    engine, broker, _ = make_engine(store, clock, config)
    store.pause(False)
    monkeypatch.setattr(engine, "_quote_map", lambda symbols: {})
    calls = []
    monkeypatch.setattr(engine, "_run_active_research",
                        lambda session, now, account, held, plans, reason: calls.append(reason))
    return engine, broker, calls


def test_missing_plan_respects_shared_expert_cooldown(research_engine, clock, monkeypatch):
    engine, broker, calls = research_engine
    monkeypatch.setattr(engine, "_run_patrol", lambda *args: pytest.fail("unexpected patrol"))
    assert engine.tick() == "active_no_plan"
    assert calls == ["plan_missing"]
    clock.advance(119)
    engine.tick()
    assert calls == ["plan_missing"]
    clock.advance(1)
    engine.tick()
    assert calls == ["plan_missing", "plan_missing"]
    assert broker.submissions == 0


def test_valid_plan_patrol_escalates_once_and_respects_cooldown(research_engine, clock, monkeypatch):
    engine, _, calls = research_engine
    monkeypatch.setattr(engine.store, "active_plans", lambda day: [{"plan_id": "x"}])
    monkeypatch.setattr(engine, "_run_patrol", lambda *args: [
        {"category": "BREAKOUT", "symbol": "CRWV", "severity": "HIGH"},
    ])
    engine.tick()
    assert calls == ["patrol:BREAKOUT:CRWV"]
    clock.advance(10)
    engine.tick()
    assert calls == ["patrol:BREAKOUT:CRWV"]


@pytest.mark.parametrize(("position", "marks", "expected"), [
    ({"symbol": "CRWV", "unrealized_plpc": 0.06}, {}, "profit_review:CRWV:5"),
    ({"symbol": "CRWV", "current_price": 103}, {"CRWV": 100}, "price_structure:CRWV"),
])
def test_python_event_does_not_also_wake_patrol_expert(research_engine, clock, monkeypatch, position, marks, expected):
    engine, _, calls = research_engine
    engine._reset_research_day("2026-11-05")
    engine._research_marks = marks
    monkeypatch.setattr(engine.store, "active_plans", lambda day: [{"plan_id": "x"}])
    monkeypatch.setattr(engine, "_account_context", lambda account: ({"positions": [position]}, {}))
    monkeypatch.setattr(engine, "_run_patrol", lambda *args: pytest.fail("unexpected patrol"))
    engine.tick()
    assert calls == [expected]


def test_research_budget_blocks_escalation_until_new_session(research_engine, clock, monkeypatch):
    engine, _, calls = research_engine
    engine._reset_research_day("2026-11-05")
    engine._active_research_tokens = 100
    engine._profit_reviewed.add(("CRWV", 0.05))
    engine._research_marks["CRWV"] = 100
    engine.multi_agent.active_research.max_tokens = 100
    monkeypatch.setattr(engine, "_run_patrol", lambda *args: [
        {"category": "BREAKOUT", "symbol": "CRWV", "severity": "HIGH"},
    ])
    engine.tick()
    assert calls == []
    clock.set(datetime(2026, 11, 6, 14, 35, tzinfo=timezone.utc))
    engine.tick()
    assert calls == ["plan_missing"]
    assert engine._active_research_tokens == 0
    assert not engine._profit_reviewed
    assert not engine._research_marks


def test_unlimited_token_budget_does_not_stop_research(research_engine):
    engine, _, calls = research_engine
    engine._reset_research_day("2026-11-05")
    engine.multi_agent.active_research.max_tokens = None
    engine._active_research_tokens = 10_000_000
    engine.tick()
    assert calls == ["plan_missing"]


def test_slow_patrol_cannot_launch_expert_after_cutoff(research_engine, clock, monkeypatch):
    engine, broker, calls = research_engine
    clock.set(datetime(2026, 11, 5, 16, 59, 25, tzinfo=timezone.utc))
    monkeypatch.setattr(engine.store, "active_plans", lambda day: [{"plan_id": "x"}])
    def patrol(*args):
        clock.advance(5)
        return [{"category": "BREAKOUT", "symbol": "CRWV", "severity": "HIGH"}]
    monkeypatch.setattr(engine, "_run_patrol", patrol)
    assert engine.tick() == "after_cutoff"
    assert not calls
    assert broker.submissions == 0


def test_late_expert_result_does_not_replace_plan(store, clock, bundle, monkeypatch):
    from trading_bot.market_data import session_info

    engine, _, _ = make_engine(store, clock, bundle)
    clock.set(datetime(2026, 11, 5, 16, 59, 25, tzinfo=timezone.utc))
    session = session_info(clock(), bundle.execution)
    monkeypatch.setattr(engine, "_collect_universe", lambda session: ({"CRWV": object()}, [], []))
    def finish_late(*args, **kwargs):
        assert kwargs["deadline_utc"] == session.submit_cutoff
        clock.advance(5)
        return SimpleNamespace(run_id="late", plan=object(), focus=[], usage=SimpleNamespace(tokens=50))
    monkeypatch.setattr(engine, "_multi_agent_premarket", finish_late)
    monkeypatch.setattr(engine, "_save_plans", lambda *args, **kwargs: pytest.fail("late plan was saved"))
    assert engine._run_active_research(session, clock(), {}, {}, [], "test") is None
    assert engine._active_research_tokens == 50
    assert store.recent_events(("active_research_failed",), 1)[0]["payload"]["run_id"] == "late"
