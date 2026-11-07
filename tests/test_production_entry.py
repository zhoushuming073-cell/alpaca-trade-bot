"""Production routing tests with no network or Alpaca order submission."""

from types import SimpleNamespace

from trading_bot.broker import PaperGateway
from trading_bot.dry_run import FakeBroker, ScriptedAI
from trading_bot.decision_schema import OrderIntent
from trading_bot.one_shot_engine import OneShotDecision, OneShotEngine
from trading_bot.strategy import PaperStrategy


class OfflinePaperGateway(PaperGateway, FakeBroker):
    """Satisfies the production gateway type without touching Alpaca."""

    def __init__(self, strategy, store, symbols, execution):
        FakeBroker.__init__(self, fill=False)
        self.store = store
        self.symbols = set(symbols)
        self.execution = execution

    def snapshot(self, include_history=False):
        result = FakeBroker.snapshot(self, include_history=include_history)
        if hasattr(self, "offline_clock"):
            result["asof"] = self.offline_clock().isoformat()
        return result
    lookup = FakeBroker.lookup
    prepare = FakeBroker.prepare
    dispatch = FakeBroker.dispatch
    quote = FakeBroker.quote
    cancel = FakeBroker.cancel


class NoThread:
    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        pass

    def stop(self):
        pass


def test_formal_lumibot_entry_uses_only_one_shot(monkeypatch, store):
    import trading_bot.strategy as strategy_module

    monkeypatch.setattr(strategy_module, "PaperGateway", OfflinePaperGateway)
    monkeypatch.setattr(strategy_module, "DeepSeek", lambda *args, **kwargs: ScriptedAI())
    monkeypatch.setattr(strategy_module, "credentials", lambda: {"DEEPSEEK_API_KEY": "offline"})
    monkeypatch.setattr(strategy_module, "CutoffWatchdog", NoThread)
    monkeypatch.setattr(strategy_module, "OneShotScheduler", NoThread)
    strategy = SimpleNamespace(set_market=lambda market: None, daily_history=lambda symbol: None)
    PaperStrategy.initialize(strategy, store)
    assert store.paused() is True
    assert store.get_control("autopilot") == "false"
    assert type(strategy.engine) is OneShotEngine
    assert strategy.engine.paper is True
    assert type(strategy.gateway) is OfflinePaperGateway
    called = []
    strategy.engine.tick = lambda: called.append("one_shot")
    PaperStrategy.on_trading_iteration(strategy)
    assert called == ["one_shot"]
    assert strategy.gateway.submissions == 0


def test_one_shot_accepts_paper_gateway_through_order_service(store, bundle, clock):
    gateway = OfflinePaperGateway(None, store, bundle.symbols, bundle.execution)
    gateway.offline_clock = clock
    engine = OneShotEngine(store, gateway, None, bundle, clock=clock,
                           require_watchdog=False, check_disk_config=False)
    store.pause(False)  # Explicitly enabled only in the isolated offline DB.
    context = engine.context()
    decision = OneShotDecision(
        decision_id="paper-adapter-offline", snapshot_id=context["snapshot_id"],
        action="PLACE_ORDER", reason="Adapter routing test",
        order_intent=OrderIntent(action="BUY", symbol="CRWV", qty=1, limit_price="100.00"),
    )
    result = engine.apply_decision(decision, context)
    assert result["state"] == "ACCEPTED"
    assert gateway.submissions == 1
    assert store.order_row(result["cid"])["state"] == "ACCEPTED"


def test_paper_decision_stops_when_recent_order_history_is_missing(store, bundle, clock):
    gateway = OfflinePaperGateway(None, store, bundle.symbols, bundle.execution)
    gateway.offline_clock = clock
    original_snapshot = gateway.snapshot

    def incomplete_snapshot(include_history=False):
        result = original_snapshot(include_history=include_history)
        if include_history:
            result["recent_orders_error"] = "timeout"
        return result

    gateway.snapshot = incomplete_snapshot
    engine = OneShotEngine(store, gateway, ScriptedAI(), bundle, clock=clock,
                           require_watchdog=False, check_disk_config=False)
    assert engine.tick()["status"] == "FAILED"
    assert gateway.submissions == 0
