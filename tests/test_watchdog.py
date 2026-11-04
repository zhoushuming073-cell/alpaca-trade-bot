from datetime import datetime,timezone
from trading_bot.watchdog import CutoffWatchdog
from trading_bot.dry_run import FakeBroker

def test_independent_cutoff_worker(store,monkeypatch):
    import trading_bot.watchdog as module
    class Clock:
        @staticmethod
        def now(tz): return datetime(2026,11,4,14,59,30,tzinfo=timezone.utc)
    monkeypatch.setattr(module,"datetime",Clock)
    b=FakeBroker()
    store.reserve("fixture","2026-11-04",{})
    b.cancel=lambda cid:b.orders.update({cid:{"status":"canceled"}})
    worker=CutoffWatchdog(store.path,b)
    steps=iter([False,True])
    worker.stop_event.wait=lambda seconds:next(steps)
    worker.run()
    assert not store.pending()
