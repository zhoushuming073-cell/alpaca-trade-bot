from datetime import datetime, timezone
import threading

from lumibot.strategies import Strategy

from .broker import PaperGateway
from .deepseek_client import DeepSeek
from .one_shot_engine import OneShotEngine
from .execution import OrderService
from .research import ResearchService, UnavailableSources
from .settings import config_snapshot, credentials, load_config
from .watchdog import CutoffWatchdog
from .one_shot_scheduler import OneShotScheduler


class PaperStrategy(Strategy):
    def initialize(self, store):
        if store.get_control("paused") is None:
            store.pause(True)
            store.set_control("autopilot", "false")
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
        self.daily_lock = threading.Lock()
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
        def context_provider(context):
            from .market_data import compact_history

            symbols = context.get("focus_symbols") or list(self.bundle.symbols)
            result = {"status": "AVAILABLE", "as_of_utc": context["as_of_utc"],
                      "symbols": {}, "profiles": self.research.summaries(symbols),
                      "news": self.research.latest_news(symbols)}
            for symbol in symbols:
                item = {"as_of_utc": context["as_of_utc"]}
                try:
                    daily = self.daily_history(symbol)
                    item["history"] = compact_history(daily)
                except Exception as exc:
                    item["history_error"] = type(exc).__name__
                if context["session"]["phase"] == "ACTIVE":
                    try:
                        item["quote"] = self.gateway.quote(symbol, self.bundle.risk)
                    except Exception as exc:
                        item["quote_error"] = type(exc).__name__
                result["symbols"][symbol] = item
            if not any("history" in item or "quote" in item for item in result["symbols"].values()):
                result["status"] = "UNAVAILABLE"
            return result

        self.engine = OneShotEngine(
            store, self.gateway, self.ai, self.bundle,
            context_provider=context_provider, require_watchdog=True,
        )
        self.watchdog = CutoffWatchdog(
            store.path, self.gateway, self.bundle.execution,
            interval_seconds=self.bundle.execution.watchdog_interval_seconds,
        )
        self.watchdog.start()
        try:
            result = OrderService(store, self.gateway, self.bundle.execution).reconcile()
            store.event("startup_reconciliation", result)
            if result.get("errors") or result.get("unknown") or result.get("conflicts"):
                store.error("startup_reconciliation", RuntimeError("Incomplete reconciliation"),
                            outcome="wait_for_watchdog_recovery")
        except Exception as exc:  # noqa: BLE001
            store.block("STARTUP_RECONCILIATION_UNAVAILABLE: " + type(exc).__name__)
            store.error("startup_reconciliation", exc, outcome="startup_blocked")
        store.heartbeat("worker", ok=True, detail={"phase": "initialized"})
        self.premarket_scheduler = OneShotScheduler(self.engine)
        self.premarket_scheduler.start()

    def on_trading_iteration(self):
        self.engine.tick()

    def before_market_opens(self):
        """Use the same idempotent path as the independent premarket fallback."""
        if not hasattr(self, "engine"):
            return
        if not hasattr(self, "premarket_scheduler"):
            self.premarket_scheduler = OneShotScheduler(self.engine)
        return self.premarket_scheduler.run_once()

    def after_market_closes(self):
        try:
            result = OrderService(
                self.engine.store, self.gateway, self.bundle.execution
            ).reconcile()
            self.engine.store.event("after_close_reconciliation", result)
        except Exception as exc:  # noqa: BLE001
            self.engine.store.error("after_close_reconciliation", exc, outcome="after_close_failed")

    def on_abrupt_closing(self):
        if hasattr(self, "premarket_scheduler"):
            self.premarket_scheduler.stop()
        if hasattr(self, "watchdog"):
            self.watchdog.stop()
        if hasattr(self, "engine"):
            self.engine.store.event("stopped", {"reason": "operator_shutdown"})

    def daily_history(self, symbol):
        from zoneinfo import ZoneInfo

        from .market_data import daily_history

        day = datetime.now(ZoneInfo("America/New_York")).date()
        key = (day, symbol)
        # LumiBot's historical-data facade is shared by the bounded premarket
        # worker pool; serialize its calls instead of assuming thread safety.
        with self.daily_lock:
            if key not in self.daily_cache:
                self.daily_cache = {k: v for k, v in self.daily_cache.items() if k[0] == day}
                raw = self.get_historical_prices(symbol, 160, timestep="day").df
                # Only validated completed sessions are cached for the day.
                self.daily_cache[key] = daily_history(raw, datetime.now(timezone.utc))
            return self.daily_cache[key]
