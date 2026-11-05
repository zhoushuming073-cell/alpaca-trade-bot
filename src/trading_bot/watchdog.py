"""Independent order reconciliation and watchdog health.

The watchdog is a read-only reconciler in the new contract.  It never cancels a
DAY order or a broker-side stop at 10:00.  It keeps the process honest by
recording a heartbeat, consecutive failures and a final dead/alive flag, so a
dead thread cannot look healthy from the main process.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone

from .execution import OrderService
from .storage import Store


class CutoffWatchdog:
    COMPONENT = "watchdog"

    def __init__(self, path, broker, execution, *, clock=None, interval_seconds=10):
        self.path = path
        self.broker = broker
        self.execution = execution
        self.clock = clock
        self.interval_seconds = max(2, int(interval_seconds))
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.run, name="order-watchdog", daemon=True)
        self.consecutive_failures = 0

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def run(self):
        store = None
        try:
            store = Store(self.path, role="watchdog", clock=self.clock)
            store.set_alive(self.COMPONENT, True)
            service = OrderService(store, self.broker, self.execution, clock=self.clock)
            while not self.stop_event.wait(self.interval_seconds):
                try:
                    # A heartbeat before a potentially slow network reconcile
                    # keeps a healthy-but-slow broker query from freezing the
                    # worker's new-risk gate.
                    store.touch(self.COMPONENT, {"phase": "start"})
                    result = service.reconcile(cancel_lingering=False)
                    self.consecutive_failures = 0
                    store.heartbeat(self.COMPONENT, ok=True, detail=result)
                except Exception as exc:  # noqa: BLE001 - loop isolation is the point
                    self.consecutive_failures += 1
                    try:
                        store.heartbeat(
                            self.COMPONENT, ok=False,
                            error=f"{type(exc).__name__}: {exc}",
                        )
                        store.error("watchdog_loop", exc, outcome="watchdog_iteration_failed")
                    except Exception:  # noqa: BLE001 - DB itself may be unavailable
                        pass
                    if self.consecutive_failures >= 3:
                        try:
                            store.block("WATCHDOG_UNHEALTHY: repeated reconciliation failures")
                        except Exception:  # noqa: BLE001
                            pass
        finally:
            if store is not None:
                try:
                    store.set_alive(self.COMPONENT, False)
                finally:
                    store.db.close()
