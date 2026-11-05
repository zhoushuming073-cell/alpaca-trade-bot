from trading_bot.dry_run import FakeBroker
from trading_bot.watchdog import CutoffWatchdog


def test_independent_watchdog_is_read_only(store, bundle, clock):
    broker = FakeBroker(fill=False)
    worker = CutoffWatchdog(
        store.path, broker, bundle.execution, clock=clock, interval_seconds=2
    )
    steps = iter([False, True])
    worker.stop_event.wait = lambda seconds: next(steps)
    worker.run()
    assert broker.cancel_calls == []
    assert broker.submissions == 0
