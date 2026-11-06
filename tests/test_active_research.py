from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from trading_bot.engine import Engine


def due_engine(*, last=None, plans=(), positions=(), tokens=0, marks=None):
    engine = SimpleNamespace(
        multi_agent=SimpleNamespace(enabled=True, active_research=SimpleNamespace(
            interval_seconds=900, min_cooldown_seconds=120, max_tokens=None)),
        ai=object(),
        execution=SimpleNamespace(),
        _active_research_day="2026-11-06",
        _last_active_research=last,
        _active_research_tokens=tokens,
        _research_marks=marks or {},
        _profit_reviewed=set(),
        _trim_news=lambda: {},
    )
    account = {"positions": list(positions)}
    return Engine._active_research_due(engine, datetime(2026, 11, 6, 15, 0, tzinfo=timezone.utc),
                                       account, {"AAPL": 1}, list(plans))


def test_missing_premarket_plan_starts_research_immediately():
    assert due_engine(plans=()) == "plan_missing_or_premarket_failed"


def test_valid_premarket_plan_starts_fifteen_minute_cadence():
    engine = SimpleNamespace(
        multi_agent=SimpleNamespace(enabled=True, active_research=SimpleNamespace(
            interval_seconds=900, min_cooldown_seconds=120, max_tokens=None)),
        ai=object(),
        execution=SimpleNamespace(),
        _active_research_day="2026-11-06",
        _last_active_research=None,
        _active_research_tokens=0,
        _research_marks={}, _profit_reviewed=set(),
        _trim_news=lambda: {},
    )
    assert Engine._active_research_due(
        engine, datetime(2026, 11, 6, 15, 0, tzinfo=timezone.utc),
        {"positions": []}, {}, [{"plan_id": "x"}],
    ) is None
    assert engine._last_active_research is not None


def test_plan_gap_and_profit_and_price_trigger_early_refresh():
    now = datetime(2026, 11, 6, 15, 0, tzinfo=timezone.utc)
    assert due_engine(last=now - timedelta(seconds=121), plans=()) == "plan_gap"
    assert due_engine(last=now - timedelta(seconds=121), plans=[{"plan_id": "x"}],
                      positions=[{"symbol": "AAPL", "unrealized_plpc": 0.06}]) == "profit_review:AAPL:5"
    assert due_engine(last=now - timedelta(seconds=121), plans=[{"plan_id": "x"}],
                      marks={"AAPL": 100}, positions=[{"symbol": "AAPL", "current_price": 103}]) == "price_structure:AAPL"
    assert due_engine(last=now - timedelta(seconds=901), plans=[{"plan_id": "x"}]) == "fifteen_minute_refresh"


def test_unlimited_token_budget_does_not_stop_research():
    assert due_engine(tokens=10_000_000, plans=(), last=None) == "plan_missing_or_premarket_failed"
