"""Offline end-to-end validation for the new session/order contract.

No network, credentials, Paper orders or real market data are used.  The
scenario covers premarket plan, a DAY limit order that stays open past 10:00,
a later fill, next-day reconciliation and the absence of any automatic cancel.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from .dry_run import FakeBroker, ScriptedAI, _days, _minutes
from .engine import Engine
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
    with offline_only(), TemporaryDirectory(prefix="paper-bot-offline-") as directory:
        bundle = load_config()
        store = Store(Path(directory) / "simulation.sqlite")
        try:
            clock = [datetime(2026, 11, 5, 14, 5, 10, tzinfo=timezone.utc)]
            broker = FakeBroker(fill=False)
            ai = ScriptedAI(symbol=bundle.symbols[0])
            engine = Engine(
                store, broker, ai, bundle,
                data=lambda symbol: _minutes(clock[0]),
                daily_data=lambda symbol: _days(clock[0]),
                clock=lambda: clock[0],
                mode="dry-run", require_watchdog=False, check_disk_config=False,
            )
            premarket = engine.tick()
            clock[0] = datetime(2026, 11, 5, 14, 35, 10, tzinfo=timezone.utc)
            active = engine.tick()
            assert premarket == "premarket_planned", premarket
            assert active == "active_triggered", active
            assert broker.submissions == 1
            order_rows = list(store.db.execute("SELECT cid,state,status,stop_policy FROM orders"))
            assert order_rows and order_rows[0]["state"] == "ACCEPTED", order_rows
            assert order_rows[0]["stop_policy"] == "NONE"
            cid = order_rows[0]["cid"]
            # 10:05 ET: no new local order and no automatic cancellation.
            clock[0] = datetime(2026, 11, 5, 15, 5, 10, tzinfo=timezone.utc)
            after = engine.tick()
            assert after in {"closed", "after_cutoff"} or after == "closed", after
            assert broker.cancel_calls == []
            assert store.order_row(cid)["state"] == "ACCEPTED"
            # The accepted DAY order can fill later while the bot is offline.
            broker.advance_fill(cid)
            # Next trading day premarket reconciles the fill before planning.
            clock[0] = datetime(2026, 11, 6, 14, 5, 10, tzinfo=timezone.utc)
            next_day = engine.tick()
            assert next_day == "premarket_planned", next_day
            row = store.order_row(cid)
            assert row["state"] == "FILLED", row["state"]
            assert broker.positions.get(bundle.symbols[0]) == 3
            assert store.pending() == [] or all(
                store.order_row(item)["state"] not in {"SUBMISSION_OUTCOME_UNKNOWN", "RECONCILIATION_REQUIRED"}
                for item in store.pending()
            )
            inputs = [
                store.payload_blob(row["id"])
                for row in store.db.execute("SELECT id FROM events WHERE kind='input'")
            ]
            return {
                "network": "disabled",
                "real_or_paper_orders_submitted": 0,
                "fixed_pool_size": len(bundle.symbols),
                "session_results": [premarket, active, after, next_day],
                "simulated_posts": broker.submissions,
                "later_day_fill": "reconciled_next_premarket",
                "auto_cancel_calls": len(broker.cancel_calls),
                "stop_policy": row["stop_policy"],
                "stop_status": row["stop_status"],
                "final_position_qty": broker.positions.get(bundle.symbols[0], 0),
                "unresolved": [item["cid"] for item in store.unresolved_orders()],
                "input_blobs": len(inputs),
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
