from datetime import datetime, timezone

from lumibot.strategies import Strategy

from .broker import PaperGateway
from .deepseek_client import DeepSeek
from .engine import Engine
from .execution import OrderService
from .research import ResearchService, UnavailableSources
from .settings import config_snapshot, credentials, load_config
from .watchdog import CutoffWatchdog


class PaperStrategy(Strategy):
    def initialize(self, store):
        self.sleeptime = "5S"
        self.set_market("NYSE")
        self.minutes_before_closing = 0
        # LumiBot calls before_market_opens 30 minutes before the session open.
        self.minutes_before_opening = 30
        self.bundle = load_config()
        # A new process is the only place a changed disk configuration becomes
        # active.  Running workers never adopt a file change silently.
        store.set_active_config(config_snapshot(self.bundle))
        store.set_config_pending(False)
        self.daily_cache = {}
        symbols = self.bundle.symbols
        self.gateway = PaperGateway(self, store, symbols, self.bundle.execution)
        keys = credentials()
        if not keys.get("DEEPSEEK_API_KEY"):
            raise ValueError("DEEPSEEK_API_KEY missing")
        self.ai = DeepSeek(
            keys["DEEPSEEK_API_KEY"],
            audit=store.event,
            record=store.ai_call,
            input_record=store.input_event,
            max_tokens=self.bundle.execution.ai_max_tokens,
            temperature=self.bundle.execution.ai_temperature,
            repair_attempts=self.bundle.execution.ai_repair_attempts,
        )
        data = lambda symbol: self.get_historical_prices(
            symbol, max(self.bundle.policy.history_bars + 40, 80),
            timestep="5 minutes", include_after_hours=False,
        ).df
        self.research = ResearchService(
            store, self.ai, data, self.daily_history, self.bundle.execution,
            sources=UnavailableSources(),
        )
        self.engine = Engine(
            store, self.gateway, self.ai, self.bundle, data,
            daily_data=self.daily_history, mode="paper", research=self.research,
            require_watchdog=True,
        )
        self.watchdog = CutoffWatchdog(
            store.path, self.gateway, self.bundle.execution,
            interval_seconds=self.bundle.execution.watchdog_interval_seconds,
        )
        self.watchdog.start()
        try:
            result = OrderService(store, self.gateway, self.bundle.execution).reconcile()
            store.event("startup_reconciliation", result)
        except Exception as exc:  # noqa: BLE001
            store.block("STARTUP_RECONCILIATION_UNAVAILABLE: " + type(exc).__name__)
            store.error("startup_reconciliation", exc, outcome="startup_blocked")

    def on_trading_iteration(self):
        self.engine.tick()

    def before_market_opens(self):
        """Run the 09:00-09:30 ET premarket plan in LumiBot's pre-open hook.

        LumiBot's scheduled on_trading_iteration normally begins at the open,
        so premarket research cannot rely on it.  This hook runs exactly at
        minutes_before_opening (30) before the actual session open and returns
        before LumiBot waits for the open.  Repeated failures stay bounded and
        leave the day without a plan rather than trading stale data.
        """
        if not hasattr(self, "engine"):
            return
        import time

        results = []
        for attempt in range(3):
            result = self.engine.tick()
            results.append(result)
            try:
                self.engine.store.event("premarket_lifecycle", {
                    "attempt": attempt + 1, "result": result,
                })
            except Exception:  # noqa: BLE001
                pass
            if result in {"premarket_planned", "premarket_no_trade", "disabled", "config_pending"}:
                break
            # A clock boundary or a transient data/AI failure can be retried
            # only while the premarket window is still open.
            from .market_data import session_info

            info = session_info(self.engine.clock(), self.engine.execution)
            if info.premarket:
                time.sleep(3)
                continue
            if info.market_open and info.now_utc < info.market_open:
                # LumiBot woke us a fraction before the 09:00 boundary.
                time.sleep(3)
                continue
            break

    def after_market_closes(self):
        try:
            result = OrderService(
                self.engine.store, self.gateway, self.bundle.execution
            ).reconcile()
            self.engine.store.event("after_close_reconciliation", result)
        except Exception as exc:  # noqa: BLE001
            self.engine.store.error("after_close_reconciliation", exc, outcome="after_close_failed")

    def on_abrupt_closing(self):
        if hasattr(self, "watchdog"):
            self.watchdog.stop()
        if hasattr(self, "engine"):
            self.engine.store.event("stopped", {"reason": "operator_shutdown"})

    def daily_history(self, symbol):
        from zoneinfo import ZoneInfo

        from .market_data import daily_history

        day = datetime.now(ZoneInfo("America/New_York")).date()
        key = (day, symbol)
        if key not in self.daily_cache:
            self.daily_cache = {k: v for k, v in self.daily_cache.items() if k[0] == day}
            raw = self.get_historical_prices(symbol, 160, timestep="day").df
            # Only validated completed sessions are cached for the day.
            self.daily_cache[key] = daily_history(raw, datetime.now(timezone.utc))
        return self.daily_cache[key]
