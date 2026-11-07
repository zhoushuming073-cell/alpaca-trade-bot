"""Schedule -> health -> resume -> real Engine + SQLite + fake AI integration.

No subprocess, external connection or broker submission is allowed.
"""
import importlib.util
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from trading_bot.premarket_scheduler import PremarketScheduler
from trading_bot.storage import Store
from trading_bot.strategy import PaperStrategy
from trading_bot.watchdog import CutoffWatchdog
from trading_bot.dry_run import FakeBroker
from test_session_engine import make_engine
from test_watchdog_health import accepted_order

ET = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("schedule_under_test", ROOT / "tools/paper_supervisor.py")
supervisor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(supervisor)


def at(hhmm, day="2026-11-06"):
    return datetime.fromisoformat(day + "T" + hhmm).replace(tzinfo=ET)


@pytest.fixture
def scheduled(tmp_path, clock, bundle, monkeypatch):
    clock.set(at("08:55:00"))
    store = Store(tmp_path / "data/paper.sqlite", clock=clock)
    store.set_control("autopilot", "true")
    store.pause(True)
    engine, broker, ai = make_engine(store, clock, bundle)
    original_complete = ai.complete
    def session_bounded_plan(mode, *args, **kwargs):
        value, meta = original_complete(mode, *args, **kwargs)
        if mode == "PREMARKET_PLAN":
            for candidate in value.candidates:
                candidate.plan_valid_until_utc = clock().replace(hour=9, minute=59, second=0)
        return value, meta
    ai.complete = session_bounded_plan
    runner = PremarketScheduler(engine)
    starts, stops, messages = [], [], []
    monkeypatch.setattr(supervisor, "ROOT", tmp_path)
    monkeypatch.setattr(supervisor, "paper_db_path", lambda: tmp_path / "data/paper.sqlite")
    monkeypatch.setattr(supervisor, "Store", lambda path, **kw: Store(path, clock=clock, **kw))
    monkeypatch.setattr(supervisor, "load_config", lambda: bundle)
    monkeypatch.setattr(supervisor, "log", messages.append)
    monkeypatch.setattr(supervisor, "ensure_dashboard", lambda: True)
    monkeypatch.setattr(supervisor, "restart_existing_worker", lambda: stops.append(clock()) or True)

    def healthy_start():
        starts.append(clock())
        store.heartbeat("worker", ok=True, detail={"phase": "initialized"})
        store.heartbeat("watchdog", ok=True, detail={"errors": 0})
        return True
    monkeypatch.setattr(supervisor, "start_worker_with_retry", healthy_start)
    state = SimpleNamespace(store=store, engine=engine, broker=broker, ai=ai, runner=runner,
                            starts=starts, stops=stops, messages=messages)
    yield state
    store.close()


def run(clock, **kwargs):
    return supervisor.run_session(clock(), supervisor.session_for(clock().date()),
                                  clock=clock, sleep=clock.advance, keep_alive=False, **kwargs)


@pytest.mark.parametrize("start", ["08:50:00", "08:55:00", "09:02:00", "09:20:00"])
def test_daily_start_generates_plan_without_lumibot_hook(scheduled, clock, start):
    s = scheduled
    clock.set(at(start))
    assert run(clock) == 0
    assert not s.store.paused()
    assert s.starts == s.stops == [max(at(start), at("08:55:00"))]
    assert s.ai.calls == []
    clock.set(max(clock(), at("09:00:00")))
    assert s.runner.run_once() == "premarket_planned"
    assert s.ai.calls == ["UNIVERSE_SCAN", "PREMARKET_PLAN"]
    assert s.store.active_plans("2026-11-06")
    assert s.broker.submissions == 0


def test_health_delayed_past_hook_then_premarket_catches_up(scheduled, clock, monkeypatch):
    s = scheduled
    def start():
        s.store.heartbeat("worker", ok=True)
        s.store.heartbeat("watchdog", ok=False, error="temporary outage")
        return True
    monkeypatch.setattr(supervisor, "start_worker_with_retry", start)
    hook_calls = []
    def sleep(seconds):
        clock.advance(seconds)
        s.store.heartbeat("worker", ok=True)
        if clock() == at("09:00:00"):
            strategy = PaperStrategy.__new__(PaperStrategy)
            strategy.engine, strategy.premarket_scheduler = s.engine, s.runner
            hook_calls.append(strategy.before_market_opens())
        if clock() >= at("09:02:00"):
            s.store.heartbeat("watchdog", ok=True, detail={"errors": 0})
    assert supervisor.run_session(clock(), supervisor.session_for(clock().date()),
                                  clock=clock, sleep=sleep, keep_alive=False) == 0
    assert hook_calls == ["premarket_blocked"]
    assert clock() == at("09:02:00")
    assert not s.store.paused()
    assert s.runner.run_once() == "premarket_planned"
    assert s.broker.submissions == 0


def test_no_healthy_watchdog_never_resumes_after_open(scheduled, clock, monkeypatch):
    s = scheduled
    clock.set(at("09:29:50"))
    monkeypatch.setattr(supervisor, "start_worker_with_retry", lambda: s.store.heartbeat("worker", ok=True) or True)
    assert run(clock) == 1
    assert clock() == at("09:30:00")
    assert s.store.paused()
    assert s.runner.run_once() == "outside_premarket"
    assert s.ai.calls == [] and s.broker.submissions == 0


@pytest.mark.parametrize("block", ["autopilot", "freeze", "config"])
def test_operator_gates_do_not_start_or_resume(scheduled, clock, block):
    s = scheduled
    if block == "autopilot":
        s.store.set_control("autopilot", "false")
    elif block == "freeze":
        s.store.block("operator freeze")
    else:
        s.store.set_config_pending(True)
    assert run(clock) == 1
    assert s.store.paused() and not s.starts and not s.stops


def test_autopilot_revoked_during_health_wait(scheduled, clock, monkeypatch):
    s = scheduled
    def start():
        s.store.set_control("autopilot", "false")
        return True
    monkeypatch.setattr(supervisor, "start_worker_with_retry", start)
    assert run(clock) == 1
    assert s.store.paused()


def test_hook_fallback_and_restart_do_not_duplicate_plan(scheduled, clock, bundle):
    s = scheduled
    s.store.pause(False)
    clock.set(at("09:00:00"))
    strategy = PaperStrategy.__new__(PaperStrategy)
    strategy.engine, strategy.premarket_scheduler = s.engine, s.runner
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(strategy.before_market_opens), pool.submit(s.runner.run_once)]
        results = [f.result() for f in futures]
    assert sorted(results) == ["premarket_not_claimed", "premarket_planned"]
    restarted, broker, ai = make_engine(s.store, clock, bundle)
    assert PremarketScheduler(restarted).run_once() == "premarket_not_claimed"
    assert s.ai.calls == ["UNIVERSE_SCAN", "PREMARKET_PLAN"] and ai.calls == []
    assert s.broker.submissions == broker.submissions == 0


def test_background_loop_catches_resume_without_hook(scheduled, clock):
    s = scheduled
    clock.set(at("08:59:50"))
    def wait(seconds):
        clock.advance(seconds)
        if clock() == at("09:00:05"):
            s.store.pause(False)
        return clock() >= at("09:00:20")
    s.runner.stop_event.wait = wait
    s.runner.run()
    assert s.ai.calls == ["UNIVERSE_SCAN", "PREMARKET_PLAN"]
    assert s.store.active_plans("2026-11-06") and s.broker.submissions == 0


def test_existing_windows_worker_tree_stops_before_replacement(monkeypatch):
    calls = []
    child = SimpleNamespace(terminate=lambda: calls.append("child"))
    parent = SimpleNamespace(children=lambda recursive: [child], terminate=lambda: calls.append("parent"))
    monkeypatch.setattr(supervisor, "worker_processes", lambda: [parent])
    monkeypatch.setattr("psutil.wait_procs", lambda processes, timeout: (processes, []))
    assert supervisor.restart_existing_worker()
    assert calls == ["child", "parent"]


def test_transient_ai_failure_retries_after_durable_cooldown(scheduled, clock, bundle):
    s = scheduled
    clock.set(at("09:00:00"))
    s.store.pause(False)
    original = s.ai.complete
    failures = [True]
    def flaky(*args, **kwargs):
        if failures:
            failures.pop()
            raise TimeoutError("mock AI outage")
        return original(*args, **kwargs)
    s.ai.complete = flaky
    assert s.runner.run_once() == "premarket_failed"
    clock.advance(5)
    assert s.runner.run_once() == "premarket_not_claimed"
    clock.advance(bundle.execution.premarket_retry_seconds - 5)
    assert s.runner.run_once() == "premarket_planned"
    assert s.broker.submissions == 0


@pytest.mark.parametrize("instant", [at("08:59:59"), at("09:30:00"), at("09:35:00"),
                                     at("09:05:00", "2026-11-07")])
def test_fallback_never_calls_ai_or_trades_outside_premarket(scheduled, clock, instant):
    s = scheduled
    clock.set(instant)
    s.store.pause(False)
    assert s.runner.run_once() == "outside_premarket"
    assert s.ai.calls == [] and s.broker.submissions == 0


def test_clock_crosses_open_before_engine_lock_is_acquired(scheduled, clock, monkeypatch):
    s = scheduled
    clock.set(at("09:29:59"))
    s.store.pause(False)
    real_tick = s.engine.premarket_tick
    def delayed():
        clock.advance(2)
        return real_tick()
    monkeypatch.setattr(s.engine, "premarket_tick", delayed)
    assert s.runner.run_once() == "outside_premarket"
    assert s.ai.calls == [] and s.broker.submissions == 0


def test_slow_ai_finishing_after_open_cannot_publish_plan(scheduled, clock):
    s = scheduled
    clock.set(at("09:29:50"))
    s.store.pause(False)
    original = s.ai.complete
    def slow(mode, *args, **kwargs):
        value = original(mode, *args, **kwargs)
        if mode == "PREMARKET_PLAN":
            clock.advance(15)
        return value
    s.ai.complete = slow
    assert s.runner.run_once() == "premarket_failed"
    assert not s.store.active_plans("2026-11-06") and s.broker.submissions == 0


@pytest.mark.parametrize("day,utc_hour", [("2026-07-06", 13), ("2026-11-06", 14)])
def test_dst_calendar_and_premarket_boundary(scheduled, clock, day, utc_hour):
    clock.set(at("09:00:00", day))
    scheduled.store.pause(False)
    opening, _ = supervisor.session_for(clock().date())
    assert opening.hour == 9 and opening.minute == 30
    assert (opening - timedelta(minutes=30)).astimezone(ZoneInfo("UTC")).hour == utc_hour
    assert scheduled.runner.run_once() == "premarket_planned"
    assert scheduled.broker.submissions == 0


def test_real_network_reconciliation_failure_is_not_healthy(store, clock, bundle):
    accepted_order(store)
    class Offline(FakeBroker):
        def lookup(self, cid):
            raise TimeoutError("mock lookup outage")
        def snapshot(self):
            raise TimeoutError("mock snapshot outage")
    worker = CutoffWatchdog(store.path, Offline(), bundle.execution, clock=clock)
    steps = iter([False, False, False, True])
    worker.stop_event.wait = lambda seconds: next(steps)
    worker.run()
    health = store.health("watchdog")
    assert health["consecutive_failures"] == 3 and health["last_success"] is None
    assert "WATCHDOG_UNHEALTHY" in store.safety_reason()

@pytest.mark.parametrize('instant,refresh', [('08:30:00', True), ('19:00:00', True), ('09:35:00', False)])
def test_idle_liveness_does_not_mask_active_engine_failure(scheduled, clock, instant, refresh):
    clock.set(at(instant))
    scheduled.store.heartbeat('worker', ok=False, error='prior engine failure')
    scheduled.runner.run_once()
    health = scheduled.store.health('worker', 90)
    assert (health['consecutive_failures'] == 0) is refresh
    if refresh:
        assert health['detail']['phase'] == 'idle'
