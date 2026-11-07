"""Offline end-to-end validation for OneShotEngine.

No network, credentials, Paper orders or real market data are used.  The
scenario covers a one-shot DAY limit order that stays open past 12:00,
a later fill, next-day reconciliation and the absence of any automatic cancel.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from .dry_run import FakeBroker, ScriptedAI
from .settings import load_config
from .storage import Store


@contextmanager
def offline_only():
    import socket

    original_connect, original_create = socket.socket.connect, socket.create_connection

    def forbidden(*args, **kwargs):
        raise AssertionError("Network forbidden in offline validation")

    socket.socket.connect, socket.create_connection = forbidden, forbidden
    try:
        yield
    finally:
        socket.socket.connect, socket.create_connection = original_connect, original_create


def run_validation():
    from .one_shot_engine import OneShotEngine
    with offline_only(), TemporaryDirectory(prefix="paper-bot-offline-") as directory:
        bundle = load_config()
        store = Store(Path(directory) / "simulation.sqlite")
        try:
            clock = [datetime(2026, 11, 5, 14, 35, 10, tzinfo=timezone.utc)]
            broker = FakeBroker(fill=False)
            ai = ScriptedAI(symbol=bundle.symbols[0])
            engine = OneShotEngine(store, broker, ai, bundle, clock=lambda: clock[0],
                                   require_watchdog=False, check_disk_config=False)
            first = engine.tick()
            assert first["state"] == "ACCEPTED", first
            cid = first["cid"]
            assert broker.submissions == 1
            clock[0] = datetime(2026, 11, 5, 17, 5, 10, tzinfo=timezone.utc)
            after = engine.tick()
            assert after["status"] == "OBSERVE_ONLY"
            assert broker.cancel_calls == []
            broker.advance_fill(cid)
            clock[0] = datetime(2026, 11, 6, 14, 5, 10, tzinfo=timezone.utc)
            next_day = engine.tick()
            assert next_day["status"] in {"PREMARKET_PREPARED", "BLOCKED"}
            assert store.order_row(cid)["state"] == "FILLED"
            row = store.order_row(cid)
            return {
                "network": "disabled", "real_or_paper_orders_submitted": 0,
                "fixed_pool_size": len(bundle.symbols), "simulated_posts": broker.submissions,
                "auto_cancel_calls": len(broker.cancel_calls),
                "stop_policy": row["stop_policy"], "stop_status": row["stop_status"],
                "final_position_qty": broker.positions.get(bundle.symbols[0], 0),
                "session_results": [first["status"], after["status"], next_day["status"]],
                "ai_calls": ai.calls,
            }
        finally:
            store.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run_validation()
    output = json.dumps(result, indent=2, ensure_ascii=False)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output + "\n", encoding="utf-8")
    print(output)
