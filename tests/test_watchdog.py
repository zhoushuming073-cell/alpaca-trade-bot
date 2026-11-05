from datetime import datetime,timezone
from trading_bot.watchdog import CutoffWatchdog
from trading_bot.dry_run import FakeBroker

def test_independent_cutoff_worker(store,monkeypatch):
    b=FakeBroker()
    store.reserve("fixture","2026-11-04",{})
    b.orders["fixture"]={"status":"new"}
    b.cancel=lambda cid:b.orders.update({cid:{"status":"canceled"}})
    worker=CutoffWatchdog(store.path,b)
    steps=iter([False,True])
    worker.stop_event.wait=lambda seconds:next(steps)
    worker.run()
    assert not store.pending()
