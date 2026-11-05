import json
import sqlite3
from datetime import datetime, timezone

import pytest

from trading_bot.storage import SCHEMA_VERSION, Store


def plans_for(store, sql, params=()):
    return " ".join(str(row[-1]) for row in store.db.execute("EXPLAIN QUERY PLAN " + sql, params))


def test_high_frequency_queries_use_indexes(store):
    plan = plans_for(
        store,
        "SELECT ts,payload FROM events WHERE kind=? ORDER BY id DESC LIMIT ?",
        ("account", 400),
    )
    assert "USING INDEX" in plan
    plan = plans_for(
        store,
        "SELECT * FROM orders WHERE state IN (?,?) ORDER BY created_at",
        ("ACCEPTED", "PARTIALLY_FILLED"),
    )
    assert "USING INDEX" in plan
    plan = plans_for(
        store,
        "SELECT * FROM plans WHERE trading_day=? AND status IN (?,?)",
        ("2026-11-05", "ACTIVE", "TRIGGERED"),
    )
    assert "USING INDEX" in plan


def test_large_ai_input_is_compressed_and_reproducible(store):
    payload = {
        "as_of_utc": "2026-11-05T14:05:00+00:00",
        "symbols": ["CRWV"],
        "summary": {"CRWV": {"daily_count": 126}},
        "excluded_symbols": {},
        "strategy": {"version": "v", "analysis_rules": "x" * 1000},
        "bars": [{"timestamp": "2026-11-05T14:30:00+00:00", "open": 1, "high": 2,
                  "low": 1, "close": 2, "volume": 10} for _ in range(3000)],
        "daily_bars": {"CRWV": [{"close": 100.0} for _ in range(3000)]},
    }
    event_id = store.input_event(payload)
    summary = json.loads(list(store.db.execute(
        "SELECT payload FROM events WHERE id=?", (event_id,)
    ))[0][0])
    assert "bars" not in summary and "daily_bars" not in summary
    assert summary["symbols"] == ["CRWV"]
    assert summary["payload_blob"]["raw_bytes"] > summary["payload_blob"]["compressed_bytes"]
    blob = store.payload_blob(event_id)
    assert blob["bars"] == payload["bars"]
    assert blob["strategy"]["analysis_rules"] == payload["strategy"]["analysis_rules"]


def test_old_database_migrates_non_destructively_and_idempotently(tmp_path):
    path = tmp_path / "old.sqlite"
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE events(id INTEGER PRIMARY KEY,ts TEXT,kind TEXT,payload TEXT);
        CREATE TABLE orders(cid TEXT PRIMARY KEY,day TEXT,status TEXT,payload TEXT);
        CREATE TABLE order_requests(cid TEXT PRIMARY KEY,cycle TEXT,ts TEXT,payload TEXT);
        CREATE TABLE fills(cid TEXT, cumulative_qty TEXT, ts TEXT, payload TEXT,
                           PRIMARY KEY(cid,cumulative_qty));
        CREATE TABLE order_dispatches(cid TEXT PRIMARY KEY,ts TEXT);
        CREATE TABLE controls(key TEXT PRIMARY KEY,value TEXT);
        INSERT INTO events VALUES(1,'old','historical','{"keep":true}');
        INSERT INTO orders VALUES('old-fill','2026-11-05','filled','{}');
        INSERT INTO orders VALUES('old-open','2026-11-05','new','{}');
        INSERT INTO order_requests VALUES('old-fill','cycle','2026-11-05T14:35:00+00:00','{}');
        """
    )
    db.commit()
    db.close()
    first = Store(path, clock=lambda: datetime(2026, 11, 5, 14, 35, tzinfo=timezone.utc))
    rows = {row["cid"]: row for row in first.db.execute("SELECT * FROM orders")}
    assert rows["old-fill"]["state"] == "FILLED"
    assert rows["old-open"]["state"] == "RECONCILIATION_REQUIRED"
    assert first.db.execute("SELECT payload FROM events WHERE id=1").fetchone()[0] == '{"keep":true}'
    assert list(first.db.execute("SELECT value FROM schema_meta WHERE key='schema_version'"))[0][0] == str(SCHEMA_VERSION)
    first.close()
    second = Store(path, clock=lambda: datetime(2026, 11, 5, 14, 35, tzinfo=timezone.utc))
    assert {row["cid"]: row["state"] for row in second.db.execute("SELECT cid,state FROM orders")} == {
        "old-fill": "FILLED", "old-open": "RECONCILIATION_REQUIRED"
    }
    second.close()


def test_dashboard_status_is_bounded(tmp_path, monkeypatch):
    from trading_bot.dashboard import ORIGIN, create_app

    path = tmp_path / "web.sqlite"
    store = Store(path)
    for index in range(2000):
        store.event("risk_result", {"index": index, "outcome": "hold"})
        store.heartbeat("worker", ok=True)
    store.close()
    monkeypatch.setattr("trading_bot.dashboard.worker_processes", lambda: [])
    client = create_app(path).test_client()
    response = client.get("/api/status", base_url=ORIGIN)
    assert response.status_code == 200
    data = response.get_json()
    assert len(data["events"]) <= 50
    assert "control_token" in data
    assert data["config_pending_restart"] is False
    assert client.get("/", base_url=ORIGIN).status_code == 200
    assert client.get("/app.js", base_url=ORIGIN).status_code == 200


def test_compact_history_keeps_raw_structure_but_bounded():
    from datetime import datetime, timezone

    from trading_bot.dry_run import _days, _minutes
    from trading_bot.market_data import compact_history

    now = datetime(2026, 11, 5, 14, 35, tzinfo=timezone.utc)
    compact = compact_history(_days(now), _minutes(now))
    assert len(compact["daily_recent"]) == 60
    assert len(compact["weekly"]) <= 26
    assert len(compact["monthly"]) <= 12
    assert len(compact["intraday_recent"]) <= 78
    assert set(compact["daily_recent"][0]) >= {
        "timestamp", "open", "high", "low", "close", "volume"
    }
    assert compact["daily_rows_total"] > len(compact["daily_recent"])
