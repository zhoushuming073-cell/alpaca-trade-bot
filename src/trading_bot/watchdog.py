"""Independent monitoring and safety cancellation; unaffected by strategy pause."""
from threading import Event, Thread
from .execution import monitor
from .storage import Store


class CutoffWatchdog:
    def __init__(self, path, broker):
        self.path, self.broker = path, broker
        self.stop_event = Event()
        self.thread = Thread(target=self.run, name="order-watchdog", daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def run(self):
        store = Store(self.path)
        try:
            while not self.stop_event.wait(10):
                if store.pending():
                    # IOC should already be terminal. Cancel any lingering order,
                    # including legacy DAY, outside the window and while paused.
                    monitor(store, self.broker, cancel=True)
        finally:
            store.db.close()
