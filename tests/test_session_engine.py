from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from trading_bot.decision_schema import (
    Candidate,
    Condition,
    MarketDecision,
    OpportunityGrade,
    OrderIntent,
    PremarketPlan,
    RecheckDecision,
    RecheckResponse,
    ScanCandidate,
    SetupType,
    StopPolicy,
    TradeDecision,
    Trigger,
    TriggerPurpose,
    UniverseScan,
)
from trading_bot.dry_run import FakeBroker, ScriptedAI, _days, _minutes
from trading_bot.engine import Engine
from trading_bot.market_data import session_info
from trading_bot.settings import load_config


def make_engine(store, clock, bundle, ai=None, broker=None):
    broker = broker or FakeBroker()
    ai = ai or ScriptedAI(symbol=bundle.symbols[0])
    engine = Engine(
        store, broker, ai, bundle,
        data=lambda symbol: _minutes(clock()),
        daily_data=lambda symbol: _days(clock()),
        clock=clock, mode="dry-run", require_watchdog=False, check_disk_config=False,
    )
    return engine, broker, ai


def plan_with(now, *, trigger, valid_minutes=40, action="BUY", qty=3, stop_policy="NONE",
              stop_loss=None):
    decision = {
        "BUY": TradeDecision.BUY, "SELL": TradeDecision.SELL,
        "OPEN_LONG": TradeDecision.OPEN_LONG, "CLOSE_LONG": TradeDecision.CLOSE_LONG,
        "OPEN_SHORT": TradeDecision.OPEN_SHORT, "COVER_SHORT": TradeDecision.COVER_SHORT,
    }[action]
    return PremarketPlan(
        as_of_utc=now, model_id="fixture",
        market_decision=MarketDecision.TRADE, account_snapshot_id="snap",
        candidates=[Candidate(
            symbol="CRWV", setup_type=SetupType.PULLBACK_RECLAIM,
            opportunity_grade=OpportunityGrade.A,
            trade_decision=decision,
            analysis="fixture", plan_id="plan-regression",
            plan_valid_until_utc=now + timedelta(minutes=valid_minutes),
            triggers=[trigger],
            order_intent=OrderIntent(
                action=action, symbol="CRWV", qty=qty, limit_price=100.0,
                stop_policy=StopPolicy(stop_policy),
                stop_loss={"stop_price": stop_loss} if stop_loss else None,
            ),
        )],
    )


class FixedAI:
    model = "fixture"

    def __init__(self, plan, recheck=None):
        self.plan = plan
        self.recheck = recheck
        self.calls = []

    def complete(self, mode, payload, parser):
        self.calls.append(mode)
        now = datetime.fromisoformat(payload["as_of_utc"]).astimezone(timezone.utc)
        meta = {"mode": mode, "model": "fixture", "latency_ms": 0, "prompt_tokens": 1,
                "completion_tokens": 1, "finish_reason": "stop"}
        if mode == "UNIVERSE_SCAN":
            return UniverseScan(
                as_of_utc=now, model_id="fixture",
                account_snapshot_id=payload["account_snapshot_id"],
                candidates=[ScanCandidate(
                    symbol="CRWV", setup_type=SetupType.PULLBACK_RECLAIM,
                    opportunity_grade=OpportunityGrade.A,
                    trade_decision=TradeDecision.BUY, thesis="fixture", priority=1,
                )],
            ), meta
        if mode == "PREMARKET_PLAN":
            plan = self.plan
            plan.as_of_utc = now
            plan.account_snapshot_id = payload["account_snapshot_id"]
            return plan, meta
        if mode == "INTRADAY_RECHECK" and self.recheck is not None:
            return self.recheck, meta
        raise AssertionError("unexpected AI mode " + mode)


def test_premarket_then_trigger_posts_once(store, clock, bundle):
    clock.set(datetime(2026, 11, 5, 14, 5, 10, tzinfo=timezone.utc))
    engine, broker, ai = make_engine(store, clock, bundle)
    assert engine.tick() == "premarket_planned"
    clock.set(datetime(2026, 11, 5, 14, 35, 10, tzinfo=timezone.utc))
    assert engine.tick() == "active_triggered"
    assert broker.submissions == 1
    clock.set(datetime(2026, 11, 5, 14, 35, 20, tzinfo=timezone.utc))
    assert engine.tick() in {"active_wait", "active_scan", "active_no_plan"}
    assert broker.submissions == 1
    assert len([o for o in broker.orders.values() if o["status"] == "filled"]) == 1


def test_short_plan_submits_sell_to_open(bundle, store, clock, monkeypatch):
    from dataclasses import replace

    bundle = replace(
        bundle,
        execution=bundle.execution.model_copy(update={"allow_shorting": True}),
        hashes={**bundle.hashes, "combined": "short-engine-test"},
    )
    monkeypatch.setattr("trading_bot.broker.load_config", lambda: bundle)
    monkeypatch.setattr("trading_bot.settings.load_config", lambda: bundle)
    clock.set(datetime(2026, 11, 5, 14, 5, 10, tzinfo=timezone.utc))
    now = clock()
    ai = FixedAI(plan_with(now, trigger=Trigger(
        trigger_id="short-exec", purpose=TriggerPurpose.EXECUTE_ORDER,
        all_of=[Condition(field="last", op="<=", value=101)],
    ), action="OPEN_SHORT", qty=2))
    broker = FakeBroker(shorting_enabled=True, shortable=True)
    engine, broker, _ = make_engine(store, clock, bundle, ai=ai, broker=broker)
    assert engine.tick() == "premarket_planned"
    clock.set(datetime(2026, 11, 5, 14, 35, 10, tzinfo=timezone.utc))
    assert engine.tick() == "active_triggered"
    assert broker.submissions == 1, store.recent_events(("order_not_sent", "intent_rejected", "error", "plan_rejected"), 10)
    assert broker.positions["CRWV"] == -2
    order = next(iter(broker.orders.values()))
    assert order["side"] == "sell"
    assert order["position_intent"] == "sell_to_open"


def test_plan_expires_before_trigger(store, clock, bundle):
    clock.set(datetime(2026, 11, 5, 14, 5, 10, tzinfo=timezone.utc))
    now = clock()
    ai = FixedAI(plan_with(now, trigger=Trigger(
        trigger_id="exec", purpose=TriggerPurpose.EXECUTE_ORDER,
        all_of=[Condition(field="last", op="<=", value=101)],
    ), valid_minutes=1))
    engine, broker, _ = make_engine(store, clock, bundle, ai=ai)
    assert engine.tick() == "premarket_planned"
    clock.set(datetime(2026, 11, 5, 14, 35, 10, tzinfo=timezone.utc))
    assert engine.tick() in {"active_scan", "active_no_plan"}
    assert broker.submissions == 0
    assert store.active_plans("2026-11-05") == []


def test_config_pending_blocks_new_orders(store, clock, bundle):
    clock.set(datetime(2026, 11, 5, 14, 5, 10, tzinfo=timezone.utc))
    engine, broker, _ = make_engine(store, clock, bundle)
    assert engine.tick() == "premarket_planned"
    store.set_config_pending(True)
    clock.set(datetime(2026, 11, 5, 14, 35, 10, tzinfo=timezone.utc))
    assert engine.tick() == "config_pending"
    assert broker.submissions == 0


def test_watchdog_unhealthy_blocks_after_grace(store, clock, bundle):
    clock.set(datetime(2026, 11, 5, 14, 5, 10, tzinfo=timezone.utc))
    engine, broker, _ = make_engine(store, clock, bundle)
    engine.require_watchdog = True
    assert engine.tick() == "premarket_planned"
    clock.set(datetime(2026, 11, 5, 14, 35, 10, tzinfo=timezone.utc))
    clock.advance(120)
    assert engine.tick() in {"watchdog_unhealthy", "config_pending", "frozen"}
    assert broker.submissions == 0


def test_watchdog_alive_but_failing_blocks_new_risk(store, clock, bundle):
    clock.set(datetime(2026, 11, 5, 14, 35, 10, tzinfo=timezone.utc))
    engine, broker, _ = make_engine(store, clock, bundle)
    engine.require_watchdog = True
    store.set_alive("watchdog", True)
    for _ in range(3):
        store.heartbeat("watchdog", ok=False)
    assert engine.tick() in {"watchdog_unhealthy", "frozen", "paused"}
    assert broker.submissions == 0
    assert "WATCHDOG_UNHEALTHY" in store.safety_reason()


def test_healthy_watchdog_allows_order(store, clock, bundle):
    clock.set(datetime(2026, 11, 5, 14, 5, 10, tzinfo=timezone.utc))
    engine, broker, _ = make_engine(store, clock, bundle)
    engine.require_watchdog = True
    store.set_alive("watchdog", True)
    store.heartbeat("watchdog", ok=True)
    assert engine.tick() == "premarket_planned"
    clock.set(datetime(2026, 11, 5, 14, 35, 10, tzinfo=timezone.utc))
    store.set_alive("watchdog", True)
    store.heartbeat("watchdog", ok=True)
    assert engine.tick() == "active_triggered"
    assert broker.submissions == 1


def test_recheck_trigger_calls_ai_once_and_does_not_order(store, clock, bundle):
    clock.set(datetime(2026, 11, 5, 14, 5, 10, tzinfo=timezone.utc))
    now = clock()
    recheck = RecheckResponse(
        as_of_utc=now, plan_id="plan-regression", decision=RecheckDecision.CONTINUE,
        reason="fixture; no new order",
    )
    ai = FixedAI(plan_with(now, trigger=Trigger(
        trigger_id="recheck", purpose=TriggerPurpose.REQUEST_AI_RECHECK,
        all_of=[Condition(field="last", op=">=", value=90)],
    )), recheck=recheck)
    engine, broker, _ = make_engine(store, clock, bundle, ai=ai)
    assert engine.tick() == "premarket_planned"
    clock.set(datetime(2026, 11, 5, 14, 35, 10, tzinfo=timezone.utc))
    assert engine.tick() == "active_scan"
    assert broker.submissions == 0
    assert ai.calls.count("INTRADAY_RECHECK") == 1
    clock.set(datetime(2026, 11, 5, 14, 36, 10, tzinfo=timezone.utc))
    engine.tick()
    assert ai.calls.count("INTRADAY_RECHECK") == 1


def test_failed_recheck_does_not_consume_trigger(store, clock, bundle):
    class FailingRecheck(FixedAI):
        def complete(self, mode, payload, parser):
            if mode == "INTRADAY_RECHECK":
                self.calls.append(mode)
                raise RuntimeError("simulated AI recheck transport failure")
            return super().complete(mode, payload, parser)

    clock.set(datetime(2026, 11, 5, 14, 5, 10, tzinfo=timezone.utc))
    now = clock()
    ai = FailingRecheck(plan_with(now, trigger=Trigger(
        trigger_id="recheck", purpose=TriggerPurpose.REQUEST_AI_RECHECK,
        all_of=[Condition(field="last", op=">=", value=90)],
    )))
    engine, broker, _ = make_engine(store, clock, bundle, ai=ai)
    assert engine.tick() == "premarket_planned"
    plan = store.active_plans("2026-11-05")[0]
    clock.set(datetime(2026, 11, 5, 14, 35, 10, tzinfo=timezone.utc))
    engine.tick()
    assert not store.trigger_fired(plan["plan_id"], "recheck")
    assert store.plan(plan["plan_id"])["status"] == "ACTIVE"
    clock.advance(121)
    engine.tick()
    assert not store.trigger_fired(plan["plan_id"], "recheck")
    assert ai.calls.count("INTRADAY_RECHECK") >= 2
    assert broker.submissions == 0


def test_invalidate_trigger_prevents_order(store, clock, bundle):
    clock.set(datetime(2026, 11, 5, 14, 5, 10, tzinfo=timezone.utc))
    now = clock()
    ai = FixedAI(plan_with(now, trigger=Trigger(
        trigger_id="invalidate", purpose=TriggerPurpose.INVALIDATE_PLAN,
        all_of=[Condition(field="last", op="<=", value=101)],
    )))
    engine, broker, _ = make_engine(store, clock, bundle, ai=ai)
    assert engine.tick() == "premarket_planned"
    clock.set(datetime(2026, 11, 5, 14, 35, 10, tzinfo=timezone.utc))
    engine.tick()
    assert broker.submissions == 0
    assert store.active_plans("2026-11-05") == []


def test_broker_stop_plan_is_blocked_without_capability(store, clock, bundle):
    clock.set(datetime(2026, 11, 5, 14, 5, 10, tzinfo=timezone.utc))
    now = clock()
    ai = FixedAI(plan_with(now, trigger=Trigger(
        trigger_id="exec", purpose=TriggerPurpose.EXECUTE_ORDER,
        all_of=[Condition(field="last", op="<=", value=101)],
    ), stop_policy="BROKER_STOP", stop_loss=95))
    engine, broker, _ = make_engine(store, clock, bundle, ai=ai)
    assert engine.tick() == "premarket_no_trade"
    assert broker.submissions == 0


def test_premarket_ai_failure_is_bounded_and_no_plan(store, clock, bundle):
    class Fail:
        model = "fixture"

        def __init__(self):
            self.calls = 0

        def complete(self, mode, payload, parser):
            self.calls += 1
            raise RuntimeError("offline AI failure")

    clock.set(datetime(2026, 11, 5, 14, 5, 10, tzinfo=timezone.utc))
    ai = Fail()
    engine, broker, _ = make_engine(store, clock, bundle, ai=ai)
    assert engine.tick() == "premarket_failed"
    assert engine.tick() == "premarket_not_claimed"  # durable retry cooldown
    assert ai.calls >= 1
    clock.set(datetime(2026, 11, 5, 14, 35, 10, tzinfo=timezone.utc))
    assert engine.tick() in {"active_no_plan", "active_scan"}
    assert broker.submissions == 0


def test_disk_config_change_defers_to_safe_restart(store, clock, bundle):
    clock.set(datetime(2026, 11, 5, 14, 5, 10, tzinfo=timezone.utc))
    engine, broker, _ = make_engine(store, clock, bundle)
    engine.check_disk_config = True
    assert engine.tick() == "premarket_planned"
    store.set_active_config({
        "symbols": list(bundle.symbols), "hashes": {"combined": "old-hash"},
        "version": "old", "execution": bundle.execution.model_dump(),
    })
    clock.set(datetime(2026, 11, 5, 14, 35, 10, tzinfo=timezone.utc))
    assert engine.tick() == "config_pending"
    assert broker.submissions == 0


def test_transient_quote_failure_recovers_without_order(store, clock, bundle):
    clock.set(datetime(2026, 11, 5, 14, 5, 10, tzinfo=timezone.utc))
    engine, broker, _ = make_engine(store, clock, bundle)
    assert engine.tick() == "premarket_planned"
    calls = {"n": 0}
    original = broker.quote

    def flaky_quote(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("offline quote timeout")
        return original(*args, **kwargs)

    broker.quote = flaky_quote
    clock.set(datetime(2026, 11, 5, 14, 35, 10, tzinfo=timezone.utc))
    first = engine.tick()
    assert first in {"active_scan", "active_wait"}
    assert broker.submissions == 0
    clock.set(datetime(2026, 11, 5, 14, 35, 25, tzinfo=timezone.utc))
    second = engine.tick()
    assert second in {"active_triggered", "active_scan", "active_wait"}
    assert broker.submissions <= 1


def test_premarket_payload_includes_research_and_news(store, clock, bundle):
    class FakeResearch:
        def summaries(self, symbols):
            return {"CRWV": {"symbol": "CRWV", "status": "OK", "facts": ["real evidence"]}}

        def capabilities(self):
            return {"deepseek_search": "CONFIGURED", "deepseek_fetch": "CONFIGURED"}

        def latest_news(self, symbols, limit=6):
            return {"CRWV": [{"title": "real news", "source_url": "https://example.com",
                              "published_at_utc": "2026-11-05T13:00:00+00:00",
                              "verified": True, "summary": "fixture"}]}

    clock.set(datetime(2026, 11, 5, 14, 5, 10, tzinfo=timezone.utc))
    engine, broker, _ = make_engine(store, clock, bundle)
    engine.research = FakeResearch()
    session = session_info(clock(), bundle.execution)
    account_ctx, _ = engine._account_context(broker.snapshot())
    payload = engine._base_payload(session, account_ctx)
    assert payload["research"]["CRWV"]["facts"] == ["real evidence"]
    assert payload["news"]["CRWV"][0]["verified"] is True
    assert "news" not in payload["unavailable_context"]


def test_plan_supersede_keeps_only_latest_active(store, bundle):
    from trading_bot.settings import config_snapshot

    store.set_active_config(config_snapshot(bundle))
    day = "2026-11-05"
    for plan_id in ("p1", "p2"):
        store.save_plan({
            "plan_id": plan_id, "mode": "PREMARKET_PLAN",
            "plan_valid_until_utc": "2026-11-05T14:55:00+00:00",
            "candidates": [], "candidate": {"symbol": "CRWV"},
        }, config_hash=bundle.hash, trading_day=day)
    active = store.active_plans(day)
    assert [plan["plan_id"] for plan in active] == ["p2"]
    assert store.plan("p1")["status"] == "SUPERSEDED"


def test_plan_from_old_config_is_invalidated(store, clock, bundle):
    clock.set(datetime(2026, 11, 5, 14, 35, 10, tzinfo=timezone.utc))
    engine, broker, _ = make_engine(store, clock, bundle)
    store.save_plan({
        "plan_id": "old-config-plan", "mode": "PREMARKET_PLAN",
        "plan_valid_until_utc": "2026-11-05T14:55:00+00:00",
        "candidate": {"symbol": "CRWV", "triggers": [], "order_intent": None},
        "candidates": [],
    }, config_hash="old-config", trading_day="2026-11-05")
    assert engine.tick() in {"active_no_plan", "active_scan"}
    assert store.plan("old-config-plan")["status"] == "INVALIDATED"
    assert broker.submissions == 0


def test_lumibot_premarket_hook_uses_shared_scheduler():
    from trading_bot.strategy import PaperStrategy

    strategy = PaperStrategy.__new__(PaperStrategy)
    strategy.engine = object()
    calls = []
    strategy.premarket_scheduler = SimpleNamespace(run_once=lambda: calls.append("shared"))
    strategy.before_market_opens()
    assert calls == ["shared"]
