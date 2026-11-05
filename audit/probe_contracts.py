"""P0 acceptance probes: correct-contract assertions, intentionally exposing gaps.
Run explicitly; all network is blocked and all databases are temporary.
"""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
from datetime import datetime, timedelta, timezone
import pytest
from conftest import no_network, store, clock, bundle
from test_wire_guards import prepared_gateway, dispatch_ready, intent
from test_session_engine import make_engine, FixedAI, plan_with
from test_watchdog_health import accepted_order
from trading_bot.decision_schema import OrderIntent, Trigger, TriggerPurpose, Condition
from trading_bot.watchdog import CutoffWatchdog
from trading_bot.dry_run import FakeBroker
from trading_bot.market_data import compact_history
from trading_bot.storage import Store

@pytest.mark.parametrize("block", ["cutoff", "pause"])
def test_final_wire_must_respect_cutoff_and_pause(tmp_path, monkeypatch, bundle, block):
    import trading_bot.broker as module
    gateway, broker, db, calls, orders = prepared_gateway(tmp_path, monkeypatch)
    dispatch_ready(db, bundle)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 5, 15, 5, tzinfo=timezone.utc) if block == "cutoff" else datetime(2026, 11, 5, 14, 35, tzinfo=timezone.utc)
    monkeypatch.setattr(module, "datetime", Clock)
    if block == "pause":
        db.pause(True)
    try:
        try:
            gateway.dispatch(intent(), "cid-1")
        except Exception:
            pass
        assert calls == [], f"POST reached mock HTTP transport despite {block}"
    finally:
        db.close()

def test_real_reconcile_network_failure_must_not_mark_watchdog_success(store, clock, bundle):
    accepted_order(store)
    class Offline(FakeBroker):
        def lookup(self, cid):
            raise TimeoutError("injected read outage")
        def snapshot(self):
            raise TimeoutError("injected snapshot outage")
    worker = CutoffWatchdog(store.path, Offline(), bundle.execution, clock=clock, interval_seconds=2)
    steps = iter([False, False, False, True])
    worker.stop_event.wait = lambda seconds: next(steps)
    worker.run()
    health = store.health("watchdog")
    assert health["consecutive_failures"] >= 3 and health["last_success"] is None, health

def test_buy_must_require_explicit_stop_policy():
    with pytest.raises(ValueError):
        OrderIntent.model_validate({"action": "BUY", "symbol": "CRWV", "qty": 3, "limit_price": 100})

def prepare_plan(store, clock, bundle, *, two_triggers=False, expiry=None):
    clock.set(datetime(2026, 11, 5, 14, 5, 10, tzinfo=timezone.utc))
    plan = plan_with(clock(), trigger=Trigger(trigger_id="exec", purpose=TriggerPurpose.EXECUTE_ORDER,
                                              all_of=[Condition(field="last", op=">=", value=100)]))
    if two_triggers:
        plan.candidates[0].triggers.append(Trigger(trigger_id="review", purpose=TriggerPurpose.REQUEST_AI_RECHECK,
                                                   all_of=[Condition(field="last", op=">=", value=90)]))
    if expiry:
        plan.candidates[0].plan_valid_until_utc = expiry
    ai = FixedAI(plan)
    engine, broker, ai = make_engine(store, clock, bundle, ai=ai)
    assert engine.tick() == "premarket_planned"
    clock.set(datetime(2026, 11, 5, 14, 35, 10, tzinfo=timezone.utc))
    return engine, broker, ai

@pytest.mark.parametrize("cause", ["cooldown", "ai_failure"])
def test_required_recheck_not_completed_must_block_original_buy(store, clock, bundle, cause):
    engine, broker, ai = prepare_plan(store, clock, bundle, two_triggers=True)
    if cause == "cooldown":
        store.set_control("recheck_last_at", clock().isoformat())
    # FixedAI without a recheck result raises on recheck, like a failed request.
    engine.tick()
    clock.advance(10)
    engine.tick()
    assert broker.submissions == 0, {"submissions": broker.submissions, "AI_calls": ai.calls}

@pytest.mark.parametrize("change", ["trigger_no_longer_true", "plan_expired"])
def test_revalidate_plan_after_slow_prepare(store, clock, bundle, change):
    expiry = datetime(2026, 11, 5, 14, 35, 15, tzinfo=timezone.utc) if change == "plan_expired" else None
    engine, broker, ai = prepare_plan(store, clock, bundle, expiry=expiry)
    original = broker.prepare
    def slow_prepare(*args, **kwargs):
        result = original(*args, **kwargs)
        if change == "plan_expired":
            clock.advance(10)
        else:
            original_quote = broker.quote
            broker.quote = lambda *a, **k: {**original_quote(*a, **k), "last": 99, "bid": 98.99, "ask": 99.01}
        return result
    broker.prepare = slow_prepare
    engine.tick()
    assert broker.submissions == 0, f"dispatched after {change}"

def test_missing_bootstrap_memory_must_block_new_buy(store, clock, bundle):
    engine, broker, ai = prepare_plan(store, clock, bundle)
    assert store.research_profile("CRWV") is None
    engine.tick()
    assert broker.submissions == 0, "New BUY authorized without required bootstrap memory"

def test_in_progress_intraday_bar_must_be_marked_incomplete(store, clock, bundle):
    import pandas as pd
    from trading_bot.dry_run import _days
    engine, broker, ai = make_engine(store, clock, bundle)
    frame = pd.DataFrame({"open":[100.], "high":[101.], "low":[99.], "close":[100.], "volume":[10.]},
                         index=pd.to_datetime(["2026-11-05T14:35:00Z"]))
    engine.data = lambda symbol: frame
    result = compact_history(_days(clock()), engine._intraday_for("CRWV"))
    records = result.get("intraday_recent", [])
    assert not records or records[-1].get("is_complete") is False, records

def test_all_holdings_must_have_deep_review_priority(store, clock, bundle):
    from types import SimpleNamespace
    engine, broker, ai = make_engine(store, clock, bundle)
    names = list(bundle.symbols)
    scan = SimpleNamespace(candidates=[SimpleNamespace(symbol=s) for s in names[:12]])
    held = {s: 1 for s in names[12:32]}
    focus = engine._focus_symbols(scan, held, {"open_orders":[]}, {s: object() for s in names})
    assert set(held).issubset(focus), {"missing_holdings":sorted(set(held)-set(focus))}
