"""Offline end-to-end validation. No credentials, network or paper database."""
import json
import socket
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import pandas as pd

from .deepseek_client import DeepSeek
from .dry_run import FakeBroker, fixture_policy
from .engine import Engine
from .market_data import CALENDAR
from .settings import config
from .storage import Store
from .execution import monitor


@contextmanager
def offline_only():
    original_connect, original_create = socket.socket.connect, socket.create_connection
    def forbidden(*args, **kwargs):
        raise AssertionError("Network forbidden in offline validation")
    socket.socket.connect, socket.create_connection = forbidden, forbidden
    try:
        yield
    finally:
        socket.socket.connect, socket.create_connection = original_connect, original_create


def run_validation():
    with offline_only(), TemporaryDirectory(prefix="paper-bot-offline-") as directory:
        store = Store(Path(directory) / "simulation.sqlite")
        try:
            names = config()[0]
            good, bad = names[0], names[-1]
            clock = [pd.Timestamp("2026-11-02T14:35:10Z")]
            schedule = CALENDAR.schedule("2026-05-02", "2026-11-01")
            days = pd.DataFrame({"open": 100., "high": 101., "low": 99., "close": 100., "volume": 100},
                                index=pd.DatetimeIndex(schedule.index).tz_localize("America/New_York"))
            def minutes(symbol):
                if symbol == bad:
                    raise ValueError("Injected missing intraday bar")
                index = pd.date_range("2026-10-30T16:45Z", "2026-10-30T19:55Z", freq="5min")
                index = index.append(pd.date_range("2026-11-02T14:30Z", clock[0].floor("5min")-pd.Timedelta(minutes=5), freq="5min"))
                return pd.DataFrame({"open": 100., "high": 101., "low": 99., "close": 100., "volume": 10}, index=index)
            action = ["BUY"]
            def completion(**kwargs):
                payload = json.loads(kwargs["messages"][1]["content"])
                decisions = [{"symbol": s, "action": action[0] if s == good else "HOLD",
                              "target_weight": .005 if action[0] == "BUY" and s == good else 0,
                              "confidence": .5, "reason": "synthetic offline signal", "risk_assessment": "fixture only"}
                             for s in payload["symbols"]]
                return SimpleNamespace(model="offline-deepseek-response", choices=[SimpleNamespace(
                    finish_reason="stop", message=SimpleNamespace(content=json.dumps({"decisions": decisions})))])
            client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=completion)))
            broker = FakeBroker("partial")
            policy = fixture_policy()
            policy.history_bars = 40
            ai = DeepSeek("offline-placeholder", client=client, audit=store.event)
            engine = Engine(store, broker, ai, names, policy, config()[2], minutes,
                            daily_data=lambda s: days, clock=lambda: clock[0], mode="dry-run")
            first = engine.tick()
            assert first == "complete" and broker.positions[good] == 2
            duplicate = engine.tick()
            assert duplicate == "duplicate" and broker.submissions == 1
            store.db.close()
            store = Store(Path(directory) / "simulation.sqlite")
            engine.store = store
            ai.audit = store.event
            action[0] = "SELL"
            broker.outcome = "filled"
            clock[0] = pd.Timestamp("2026-11-02T14:40:10Z")
            second = engine.tick()
            assert second == "complete" and broker.positions[good] == 0 and broker.submissions == 2
            clock[0] = pd.Timestamp("2026-11-02T15:00:00Z")
            after_cutoff = engine.tick()
            assert after_cutoff == "outside_window" and broker.submissions == 2
            # A legacy DAY survives restart; safety-only cancellation works at cutoff.
            store.reserve("aip-legacy-fixture", "2026-11-02", {"symbol": good, "time_in_force": "day"}, "legacy")
            broker.orders["aip-legacy-fixture"] = {"symbol": good, "side": "buy", "qty": "1", "filled_qty": "0", "status": "new"}
            store.pause(True)
            monitor(store, broker, cancel=True)
            assert not store.pending()
            inputs = [json.loads(p) for (p,) in store.db.execute("SELECT payload FROM events WHERE kind='input'")]
            counts = dict(store.db.execute("SELECT kind,count(*) FROM events GROUP BY kind"))
            assert all(len(p["symbols"]) == 47 and len(p["excluded_symbols"]) == 1 for p in inputs)
            assert all(p["summary"][good]["intraday_count"] == 40 for p in inputs)
            assert all(p["summary"][good]["daily_count"] == len(schedule) for p in inputs)
            assert counts["ai_raw"] == counts["decision"] == 2
            assert store.db.execute("SELECT count(*) FROM fills").fetchone()[0] == 2
            return {"network": "disabled", "real_or_paper_orders_submitted": 0, "fixed_pool_size": len(names),
                    "valid_per_cycle": 47, "excluded_per_cycle": 1, "daily_bars_per_symbol": len(schedule),
                    "intraday_bars_per_symbol": 40, "cycle_results": [first, duplicate, second, after_cutoff],
                    "simulated_posts": broker.submissions, "buy_requested_qty": 5, "buy_filled_qty": 2,
                    "sell_requested_qty": 2, "sell_filled_qty": 2, "final_position_qty": broker.positions[good],
                    "legacy_order": "canceled_after_cutoff_while_paused", "unresolved": store.pending(),
                    "audit_event_counts": counts, "restart_recovery": "passed"}
        finally:
            store.db.close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run_validation()
    output = json.dumps(result, indent=2, ensure_ascii=False)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output+"\n", encoding="utf-8")
    print(output)
