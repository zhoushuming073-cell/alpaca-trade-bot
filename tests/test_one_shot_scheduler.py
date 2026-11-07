"""The independent premarket callback cannot enter trading execution."""

from datetime import datetime, timezone

from trading_bot.dry_run import FakeBroker, ScriptedAI
from trading_bot.one_shot_engine import OneShotEngine
from trading_bot.one_shot_scheduler import OneShotScheduler


def test_premarket_scheduler_research_only_then_stops_at_open(store, bundle, clock):
    clock.set(datetime(2026, 11, 5, 14, 5, 10, tzinfo=timezone.utc))
    broker = FakeBroker(fill=False)
    ai = ScriptedAI()
    engine = OneShotEngine(store, broker, ai, bundle, clock=clock,
                           require_watchdog=False, check_disk_config=False)
    scheduler = OneShotScheduler(engine)
    assert scheduler.run_once()["status"] == "PREMARKET_PREPARED"
    assert scheduler.run_once()["status"] == "PREMARKET_PREPARED"
    clock.set(datetime(2026, 11, 5, 14, 35, 10, tzinfo=timezone.utc))
    assert scheduler.run_once() == "outside_premarket"
    assert ai.calls == ["ONE_SHOT_RESEARCH"]
    assert broker.submissions == 0
