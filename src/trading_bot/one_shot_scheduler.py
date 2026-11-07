"""Retry premarket research independently of LumiBot's one-shot lifecycle hook.

This runner can only enter OneShotEngine.premarket_tick: it never starts an active
trading iteration, resumes a paused account, or sends/cancels orders.
"""
import threading

from .market_data import session_info


class OneShotScheduler:
    def __init__(self, engine, interval_seconds=5):
        self.engine = engine
        self.interval_seconds = interval_seconds
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.run, name="one-shot-premarket", daemon=True)
        self.last_result = None

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def run_once(self):
        if self.stop_event.is_set():
            return "stopped"
        info = session_info(self.engine.clock(), self.engine.config.execution)
        if not info.premarket:
            # Initialization/idle liveness must not depend on the 09:00 hook.
            # This is NOT evidence of a successful broker reconciliation.
            if not info.active:
                self.engine.store.heartbeat("worker", ok=True, detail={"phase": "idle", "reason": info.reason})
            return "outside_premarket"
        result = self.engine.premarket_tick()  # rechecks time after acquiring lock
        signature = (info.trading_day, str(result))
        if signature != self.last_result:
            self.engine.store.event("premarket_lifecycle", {
                "source": "one_shot_scheduler", "trading_day": info.trading_day,
                "result": result,
            })
            self.last_result = signature
        return result

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.run_once()
            except Exception as exc:  # keep retries alive after transient failures
                self.engine.store.error("one_shot_scheduler", exc, outcome="retry_before_open_only")
            if self.stop_event.wait(self.interval_seconds):
                return
