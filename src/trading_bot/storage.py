"""Durable SQLite storage, schema migration and the local order ledger.

Key invariants
--------------
* ``orders.state`` is the local lifecycle; ``orders.status`` retains the last
  broker status for audit/UI compatibility.
* ``order_dispatches`` is written in the same transaction as the transition to
  ``DISPATCH_INTENT_RECORDED``.  A missing dispatch row is proof that no HTTP
  POST was attempted.
* Every state change is an optimistic, versioned conditional update and is
  appended to ``order_transitions``.
* Large AI input payloads are stored as a compact summary plus a compressed
  blob, so the event table stays queryable without losing reproducibility.
* Schema creation/migration is additive and idempotent; existing databases and
  historical rows are never deleted or rewritten destructively.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import uuid
import zlib
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .order_state import (
    ALLOWED_TRANSITIONS,
    PRE_DISPATCH_STATES,
    TERMINAL_STATES,
    OrderState,
    broker_state,
    can_transition,
    is_terminal,
)
from .safety import redact

SCHEMA_VERSION = 2

# Stores created in the same process (worker + watchdog + dashboard handler)
# share this identity.  A new process gets a new identity, which is what makes
# stale-lease recovery safe.
PROCESS_INSTANCE_ID = uuid.uuid4().hex

_EVENT_SUMMARY_DROP = ("bars", "daily_bars", "intraday_bars", "research_profiles_raw")


def _iso(value: datetime | None = None) -> str:
    value = value or datetime.now(timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _json(payload: Any) -> str:
    return json.dumps(redact(payload), default=str, sort_keys=True, allow_nan=False)


def _loads(payload: str | None) -> Any:
    if not payload:
        return None
    try:
        return json.loads(payload)
    except (TypeError, ValueError):
        return None


class Store:
    """One connection per role/thread; SQLite WAL + explicit transactions."""

    def __init__(self, path, *, clock=None, instance_id: str | None = None, role: str = "worker"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.instance_id = instance_id or PROCESS_INSTANCE_ID
        self.role = role
        # isolation_level=None: every write below is an explicit BEGIN IMMEDIATE.
        self.db = sqlite3.connect(self.path, timeout=10, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=10000")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self._migrate()

    # ------------------------------------------------------------------ schema
    def _table_columns(self, table: str) -> set[str]:
        return {row[1] for row in self.db.execute(f"PRAGMA table_info({table})")}

    def _add_columns(self, table: str, columns: dict[str, str]) -> set[str]:
        existing = self._table_columns(table)
        added = set()
        for name, ddl in columns.items():
            if name not in existing:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
                added.add(name)
        return added

    def _migrate(self) -> None:
        with self.lock:
            self.db.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta(key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, ts TEXT, kind TEXT, payload TEXT);
                CREATE TABLE IF NOT EXISTS event_blobs(
                    event_id INTEGER PRIMARY KEY, encoding TEXT, body BLOB, sha256 TEXT,
                    raw_bytes INTEGER, created_at TEXT);
                CREATE TABLE IF NOT EXISTS cycles(id TEXT PRIMARY KEY, status TEXT, updated_at TEXT);
                CREATE TABLE IF NOT EXISTS sessions(
                    session_id TEXT PRIMARY KEY, trading_day TEXT, phase TEXT, status TEXT,
                    attempts INTEGER DEFAULT 0, checkpoint TEXT, updated_at TEXT);
                CREATE TABLE IF NOT EXISTS orders(
                    cid TEXT PRIMARY KEY, day TEXT, status TEXT, payload TEXT,
                    state TEXT, version INTEGER DEFAULT 0, owner TEXT, lease_until TEXT,
                    plan_id TEXT, trigger_id TEXT, intent TEXT, broker_order_id TEXT,
                    stop_policy TEXT, stop_status TEXT, stop_order_id TEXT,
                    dispatch_intent_at TEXT, not_sent_at TEXT, reason TEXT,
                    created_at TEXT, updated_at TEXT);
                CREATE TABLE IF NOT EXISTS order_requests(cid TEXT PRIMARY KEY, cycle TEXT, ts TEXT, payload TEXT);
                CREATE TABLE IF NOT EXISTS fills(
                    cid TEXT, cumulative_qty TEXT, ts TEXT, payload TEXT,
                    PRIMARY KEY(cid, cumulative_qty));
                CREATE TABLE IF NOT EXISTS order_dispatches(cid TEXT PRIMARY KEY, ts TEXT);
                CREATE TABLE IF NOT EXISTS order_transitions(
                    id INTEGER PRIMARY KEY, cid TEXT, ts TEXT, from_state TEXT, to_state TEXT,
                    owner TEXT, reason TEXT);
                CREATE TABLE IF NOT EXISTS controls(key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS plans(
                    plan_id TEXT PRIMARY KEY, trading_day TEXT, mode TEXT, status TEXT,
                    version INTEGER DEFAULT 1, payload TEXT, config_hash TEXT,
                    created_at TEXT, valid_until TEXT, superseded_by TEXT, reason TEXT);
                CREATE TABLE IF NOT EXISTS plan_triggers(
                    plan_id TEXT, trigger_id TEXT, symbol TEXT, purpose TEXT, payload TEXT,
                    fired_at TEXT, context_hash TEXT, PRIMARY KEY(plan_id, trigger_id));
                CREATE TABLE IF NOT EXISTS ai_calls(
                    id INTEGER PRIMARY KEY, ts TEXT, mode TEXT, model TEXT, prompt_hash TEXT,
                    input_hash TEXT, output_hash TEXT, prompt_tokens INTEGER,
                    completion_tokens INTEGER, latency_ms INTEGER, finish_reason TEXT,
                    outcome TEXT, plan_id TEXT, error TEXT, usage TEXT);
                CREATE TABLE IF NOT EXISTS research_profiles(
                    symbol TEXT PRIMARY KEY, version INTEGER, status TEXT, profile TEXT,
                    source_hash TEXT, created_at TEXT, verified_at TEXT, expires_at TEXT);
                CREATE TABLE IF NOT EXISTS research_evidence(
                    id INTEGER PRIMARY KEY, symbol TEXT, kind TEXT, source_url TEXT, title TEXT,
                    published_at TEXT, retrieved_at TEXT, available_at TEXT, verified INTEGER,
                    content_hash TEXT, summary TEXT, created_at TEXT);
                CREATE TABLE IF NOT EXISTS research_revisions(
                    id INTEGER PRIMARY KEY, symbol TEXT, version INTEGER, previous_hash TEXT,
                    new_hash TEXT, reason TEXT, created_at TEXT, payload TEXT);
                CREATE TABLE IF NOT EXISTS daily_scans(
                    trading_day TEXT PRIMARY KEY, status TEXT, candidates TEXT,
                    profile_hash TEXT, created_at TEXT, ai_call_id INTEGER);
                CREATE TABLE IF NOT EXISTS runtime_health(
                    component TEXT PRIMARY KEY, alive INTEGER DEFAULT 0,
                    last_heartbeat TEXT, last_success TEXT, consecutive_failures INTEGER DEFAULT 0,
                    last_error TEXT, detail TEXT);
                CREATE TABLE IF NOT EXISTS agent_runs(
                    run_id TEXT PRIMARY KEY, trading_day TEXT, mode TEXT, status TEXT,
                    as_of_utc TEXT, universe_version TEXT, config_hash TEXT, snapshot_hash TEXT,
                    baseline_status TEXT, error TEXT, created_at TEXT, updated_at TEXT);
                CREATE TABLE IF NOT EXISTS agent_reports(
                    report_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, role TEXT NOT NULL,
                    symbol TEXT DEFAULT '', status TEXT NOT NULL, schema_version TEXT,
                    model TEXT, prompt_hash TEXT, input_hash TEXT, output_hash TEXT,
                    report_json TEXT, parsed_json TEXT, error TEXT,
                    created_at TEXT, updated_at TEXT,
                    UNIQUE(run_id, role, symbol));
                CREATE TABLE IF NOT EXISTS agent_plan_links(
                    run_id TEXT, plan_id TEXT, report_id TEXT, role TEXT,
                    executed INTEGER DEFAULT 0, created_at TEXT,
                    PRIMARY KEY(run_id, plan_id));
                CREATE TABLE IF NOT EXISTS agent_feedback(
                    id INTEGER PRIMARY KEY, run_id TEXT, symbol TEXT, plan_id TEXT,
                    observed_at TEXT, outcome TEXT, payload TEXT, created_at TEXT);
                CREATE TABLE IF NOT EXISTS agent_cache(
                    cache_key TEXT PRIMARY KEY, role TEXT, model TEXT, input_hash TEXT,
                    output_json TEXT, created_at TEXT, expires_at TEXT, hits INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS agent_experiments(
                    run_id TEXT PRIMARY KEY, trading_day TEXT, snapshot_hash TEXT,
                    baseline_report_id TEXT, three_agent_plan_id TEXT,
                    baseline_decision TEXT, three_agent_decision TEXT,
                    baseline_tokens INTEGER, three_agent_tokens INTEGER,
                    baseline_latency_ms INTEGER, three_agent_latency_ms INTEGER,
                    created_at TEXT);
                """
            )
            # Additive migration for databases created by the previous release.
            added = self._add_columns(
                "orders",
                {
                    "state": "TEXT",
                    "version": "INTEGER DEFAULT 0",
                    "owner": "TEXT",
                    "lease_until": "TEXT",
                    "plan_id": "TEXT",
                    "trigger_id": "TEXT",
                    "intent": "TEXT",
                    "broker_order_id": "TEXT",
                    "stop_policy": "TEXT",
                    "stop_status": "TEXT",
                    "stop_order_id": "TEXT",
                    "dispatch_intent_at": "TEXT",
                    "not_sent_at": "TEXT",
                    "reason": "TEXT",
                    "created_at": "TEXT",
                    "updated_at": "TEXT",
                },
            )
            self._add_columns("cycles", {"updated_at": "TEXT"})
            self._add_columns(
                "ai_calls",
                {
                    "run_id": "TEXT",
                    "agent_role": "TEXT",
                    "cost_usd": "REAL",
                    "cached": "INTEGER DEFAULT 0",
                },
            )
            self.db.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_events_kind_id ON events(kind, id);
                CREATE INDEX IF NOT EXISTS idx_orders_state ON orders(state);
                CREATE INDEX IF NOT EXISTS idx_orders_day_state ON orders(day, state);
                CREATE INDEX IF NOT EXISTS idx_orders_plan ON orders(plan_id, trigger_id);
                CREATE INDEX IF NOT EXISTS idx_order_requests_cycle ON order_requests(cycle);
                CREATE INDEX IF NOT EXISTS idx_plans_day_status ON plans(trading_day, status);
                CREATE INDEX IF NOT EXISTS idx_plan_triggers_plan ON plan_triggers(plan_id, fired_at);
                CREATE INDEX IF NOT EXISTS idx_ai_calls_ts ON ai_calls(ts);
                CREATE INDEX IF NOT EXISTS idx_research_evidence_symbol ON research_evidence(symbol, id);
                CREATE INDEX IF NOT EXISTS idx_research_revisions_symbol ON research_revisions(symbol, id);
                CREATE INDEX IF NOT EXISTS idx_agent_reports_run ON agent_reports(run_id, role, symbol);
                CREATE INDEX IF NOT EXISTS idx_agent_reports_symbol ON agent_reports(symbol, created_at);
                CREATE INDEX IF NOT EXISTS idx_agent_runs_day ON agent_runs(trading_day, created_at);
                CREATE INDEX IF NOT EXISTS idx_agent_plan_links_plan ON agent_plan_links(plan_id);
                """
            )
            # Only rows from the old schema need a state mapping.  A row
            # created by this version is already in RESERVED and must never be
            # reinterpreted as an old, unresolved order on every restart.
            if "state" in added:
                for row in self.db.execute(
                    "SELECT cid,status,created_at,updated_at FROM orders"
                ).fetchall():
                    status = str(row["status"] or "").lower()
                    mapped = {
                        "filled": OrderState.FILLED.value,
                        "canceled": OrderState.CANCELED.value,
                        "expired": OrderState.EXPIRED.value,
                        "rejected": OrderState.REJECTED.value,
                    }.get(status)
                    if mapped is None:
                        # We cannot prove how an old open row was dispatched.
                        mapped = OrderState.RECONCILIATION_REQUIRED.value
                    self.db.execute(
                        "UPDATE orders SET state=?, created_at=COALESCE(created_at,?), "
                        "updated_at=COALESCE(updated_at,?) WHERE cid=?",
                        (mapped, _iso(), _iso(), row["cid"]),
                    )
            else:
                for row in self.db.execute(
                    "SELECT cid,created_at,updated_at FROM orders "
                    "WHERE created_at IS NULL OR updated_at IS NULL"
                ).fetchall():
                    self.db.execute(
                        "UPDATE orders SET created_at=COALESCE(created_at,?), "
                        "updated_at=COALESCE(updated_at,?) WHERE cid=?",
                        (_iso(), _iso(), row["cid"]),
                    )
            # A crash during an earlier migration could leave state NULL even
            # though the column exists.  Map only those rows; never reinterpret
            # a row that already has a local state.
            for row in self.db.execute(
                "SELECT cid,status FROM orders WHERE state IS NULL OR state=''"
            ).fetchall():
                status = str(row["status"] or "").lower()
                mapped = {
                    "filled": OrderState.FILLED.value,
                    "canceled": OrderState.CANCELED.value,
                    "expired": OrderState.EXPIRED.value,
                    "rejected": OrderState.REJECTED.value,
                }.get(status, OrderState.RECONCILIATION_REQUIRED.value)
                self.db.execute(
                    "UPDATE orders SET state=?, created_at=COALESCE(created_at,?), "
                    "updated_at=COALESCE(updated_at,?) WHERE cid=?",
                    (mapped, _iso(), _iso(), row["cid"]),
                )
            self.db.execute(
                "INSERT OR REPLACE INTO schema_meta(key,value) VALUES('schema_version',?)",
                (str(SCHEMA_VERSION),),
            )
            self.db.execute(
                "INSERT OR REPLACE INTO runtime_health(component,alive,last_heartbeat) "
                "SELECT 'worker',0,? WHERE NOT EXISTS "
                "(SELECT 1 FROM runtime_health WHERE component='worker')",
                (_iso(),),
            )

    # -------------------------------------------------------------- transactions
    @contextmanager
    def _tx(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self.db.rollback()
                raise
            else:
                self.db.commit()

    def close(self):
        with self.lock:
            self.db.close()

    # ------------------------------------------------------------------- events
    def _insert_event(self, kind: str, payload: dict, *, ts: str | None = None) -> int:
        cursor = self.db.execute(
            "INSERT INTO events(ts,kind,payload) VALUES(?,?,?)",
            (ts or _iso(self.clock()), kind, _json(payload)),
        )
        return int(cursor.lastrowid)

    def event(self, kind: str, payload: dict) -> int | None:
        if kind == "input":
            return self.input_event(payload)
        with self._tx():
            return self._insert_event(kind, payload)

    def input_event(self, payload: dict) -> int:
        """Store a compact queryable summary and a compressed full payload."""
        raw = _json(payload).encode("utf-8")
        compressed = zlib.compress(raw, 6)
        digest = hashlib.sha256(raw).hexdigest()
        summary = dict(payload)
        for key in _EVENT_SUMMARY_DROP:
            summary.pop(key, None)
        strategy = summary.get("strategy")
        if isinstance(strategy, dict):
            summary["strategy"] = {
                "version": strategy.get("version"),
                "hash": hashlib.sha256(
                    json.dumps(strategy, sort_keys=True, default=str).encode()
                ).hexdigest(),
            }
        summary["payload_blob"] = {
            "encoding": "zlib",
            "sha256": digest,
            "raw_bytes": len(raw),
            "compressed_bytes": len(compressed),
        }
        with self._tx():
            event_id = self._insert_event("input", summary)
            self.db.execute(
                "INSERT INTO event_blobs(event_id,encoding,body,sha256,raw_bytes,created_at)"
                " VALUES(?,?,?,?,?,?)",
                (event_id, "zlib", compressed, digest, len(raw), _iso(self.clock())),
            )
        return event_id

    def payload_blob(self, event_id: int) -> dict | None:
        with self.lock:
            row = self.db.execute(
                "SELECT encoding,body,sha256 FROM event_blobs WHERE event_id=?", (event_id,)
            ).fetchone()
        if not row:
            return None
        if row["encoding"] == "zlib":
            raw = zlib.decompress(row["body"])
        else:  # pragma: no cover - future encodings
            raw = row["body"]
        return json.loads(raw.decode("utf-8"))

    def recent_events(self, kinds: Iterable[str], limit: int = 40) -> list[dict]:
        kinds = list(dict.fromkeys(kinds))
        if not kinds:
            return []
        placeholders = ",".join("?" for _ in kinds)
        limit = max(1, min(int(limit), 500))
        sql = (
            f"SELECT id,ts,kind,payload FROM events WHERE kind IN ({placeholders}) "
            "ORDER BY id DESC LIMIT ?"
        )
        with self.lock:
            rows = self.db.execute(sql, (*kinds, limit)).fetchall()
        return [
            {"id": row["id"], "ts": row["ts"], "kind": row["kind"], "payload": _loads(row["payload"])}
            for row in rows
        ]

    def robot_events(self, kinds: Iterable[str], *, after_id: int = 0, limit: int = 200) -> list[dict]:
        """Incremental event cursor for the read-only robot terminal."""
        kinds = list(dict.fromkeys(kinds))
        if not kinds:
            return []
        placeholders = ",".join("?" for _ in kinds)
        limit = max(1, min(int(limit), 500))
        if int(after_id or 0) > 0:
            sql = (
                f"SELECT id,ts,kind,payload FROM events WHERE id>? AND kind IN ({placeholders}) "
                "ORDER BY id ASC LIMIT ?"
            )
            args = (int(after_id), *kinds, limit)
        else:
            sql = (
                f"SELECT id,ts,kind,payload FROM events WHERE kind IN ({placeholders}) "
                "ORDER BY id DESC LIMIT ?"
            )
            args = (*kinds, limit)
        with self.lock:
            rows = self.db.execute(sql, args).fetchall()
        result = [
            {"id": row["id"], "ts": row["ts"], "kind": row["kind"], "payload": _loads(row["payload"])}
            for row in rows
        ]
        if int(after_id or 0) <= 0:
            result.reverse()
        return result

    def recent_accounts(self, limit: int = 400) -> list[dict]:
        limit = max(1, min(int(limit), 2000))
        with self.lock:
            rows = self.db.execute(
                "SELECT ts,payload FROM events WHERE kind='account' ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {"ts": row["ts"], **( _loads(row["payload"]) or {}) } for row in reversed(rows)
        ]

    def recent_ai_calls(self, limit: int = 20) -> list[dict]:
        limit = max(1, min(int(limit), 200))
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM ai_calls ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def error(self, stage, exc, *, cycle=None, ticker=None, cid=None, outcome="blocked", retried=False):
        import requests

        network = isinstance(exc, (requests.RequestException, TimeoutError, ConnectionError))
        category = (
            "deepseek_request" if stage == "deepseek_request" else
            "ai_format" if stage in {"deepseek_schema", "ai_schema"} else
            "order_cancel" if stage == "order_cancel" else
            "order_unknown" if stage in {"order_unknown", "order_reconciliation"} else
            "order_submit" if stage in {"order_submit", "order_dispatch"} else
            "alpaca_network" if network else
            "market_data" if stage in {"intraday_data", "daily_data", "quote", "trigger_data", "premarket_data"} else
            stage
        )
        payload = {
            "stage": stage,
            "type": type(exc).__name__,
            "reason": str(exc),
            "cycle": cycle,
            "ticker": ticker,
            "cid": cid,
            "retried": retried,
            "outcome": outcome,
            "category": category,
        }
        self.event("error", payload)
        logging.error("%s", json.dumps(redact(payload), ensure_ascii=False))

    # ------------------------------------------------------------------ controls
    def get_control(self, key: str) -> str | None:
        with self.lock:
            row = self.db.execute("SELECT value FROM controls WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_control(self, key: str, value: str) -> None:
        with self._tx():
            self.db.execute(
                "INSERT INTO controls(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def paused(self) -> bool:
        return self.get_control("paused") == "true"

    def pause(self, value: bool) -> None:
        self.set_control("paused", "true" if value else "false")

    def safety_reason(self) -> str:
        return self.get_control("safety_reason") or ""

    def block(self, reason: str) -> None:
        reason = redact(reason)
        if self.safety_reason() != reason:
            self.event("safety_alert", {"reason": reason, "outcome": "new_orders_disabled"})
        self.set_control("safety_reason", reason)
        self.pause(True)

    freeze = block

    def acknowledge(self) -> None:
        # Broker-accepted DAY orders and stops are legitimate open facts; only
        # unknown/conflicting local state blocks an acknowledgement.
        if self.has_unknown_orders():
            raise ValueError("Cannot acknowledge with unknown or conflicting orders")
        self.event("operator", {"action": "acknowledge_safety", "reason": self.safety_reason()})
        self.set_control("safety_reason", "")

    def active_config(self) -> dict | None:
        return _loads(self.get_control("active_config"))

    def set_active_config(self, snapshot: dict) -> None:
        self.set_control("active_config", json.dumps(redact(snapshot), sort_keys=True, default=str))

    def config_pending(self) -> bool:
        return self.get_control("config_pending_restart") == "true"

    def set_config_pending(self, pending: bool) -> None:
        self.set_control("config_pending_restart", "true" if pending else "false")

    # ------------------------------------------------------------------ health
    def touch(self, component: str, detail: dict | None = None) -> None:
        """Refresh liveness without claiming a successful business iteration."""
        now = _iso(self.clock())
        with self._tx():
            self.db.execute(
                "INSERT INTO runtime_health(component,alive,last_heartbeat,detail) VALUES(?,?,?,?) "
                "ON CONFLICT(component) DO UPDATE SET alive=1,last_heartbeat=excluded.last_heartbeat,"
                "detail=excluded.detail",
                (component, 1, now, json.dumps(detail or {}, default=str)),
            )

    def heartbeat(
        self, component: str, *, ok: bool = True, error: str | None = None, detail: dict | None = None
    ) -> None:
        now = _iso(self.clock())
        with self._tx():
            row = self.db.execute(
                "SELECT consecutive_failures FROM runtime_health WHERE component=?", (component,)
            ).fetchone()
            failures = int(row[0]) if row else 0
            if ok:
                self.db.execute(
                    "INSERT INTO runtime_health(component,alive,last_heartbeat,last_success,"
                    "consecutive_failures,last_error,detail) VALUES(?,?,?,?,0,NULL,?) "
                    "ON CONFLICT(component) DO UPDATE SET alive=1,last_heartbeat=excluded.last_heartbeat,"
                    "last_success=excluded.last_success,consecutive_failures=0,last_error=NULL,"
                    "detail=excluded.detail",
                    (component, 1, now, now, json.dumps(detail or {}, default=str)),
                )
            else:
                failures += 1
                self.db.execute(
                    "INSERT INTO runtime_health(component,alive,last_heartbeat,consecutive_failures,last_error,detail)"
                    " VALUES(?,1,?,?,?,?) ON CONFLICT(component) DO UPDATE SET alive=1,"
                    "last_heartbeat=excluded.last_heartbeat,consecutive_failures=excluded.consecutive_failures,"
                    "last_error=excluded.last_error,detail=excluded.detail",
                    (component, now, failures, redact(error or ""), json.dumps(detail or {}, default=str)),
                )

    def set_alive(self, component: str, alive: bool) -> None:
        now = _iso(self.clock())
        with self._tx():
            self.db.execute(
                "INSERT INTO runtime_health(component,alive,last_heartbeat) VALUES(?,?,?) "
                "ON CONFLICT(component) DO UPDATE SET alive=excluded.alive,last_heartbeat=excluded.last_heartbeat",
                (component, 1 if alive else 0, now),
            )

    def health(self, component: str, stale_seconds: int = 45) -> dict:
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM runtime_health WHERE component=?", (component,)
            ).fetchone()
        if not row:
            return {"component": component, "alive": False, "last_heartbeat": None,
                    "last_success": None, "consecutive_failures": 0, "last_error": None,
                    "detail": {}, "stale": True}
        detail = _loads(row["detail"]) or {}
        last = _parse_iso(row["last_heartbeat"])
        stale = last is None or (self.clock() - last) > timedelta(seconds=max(1, int(stale_seconds)))
        return {
            "component": row["component"],
            "alive": bool(row["alive"]),
            "last_heartbeat": row["last_heartbeat"],
            "last_success": row["last_success"],
            "consecutive_failures": int(row["consecutive_failures"] or 0),
            "last_error": row["last_error"],
            "detail": detail,
            "stale": stale,
        }

    # ------------------------------------------------------------- order ledger
    def reserve_order(
        self,
        cid: str,
        day: str,
        intent: dict,
        *,
        plan_id: str | None = None,
        trigger_id: str | None = None,
        stop_policy: str | None = None,
        owner: str | None = None,
        lease_seconds: int = 90,
    ) -> dict:
        now = self.clock()
        stop_policy = str(stop_policy or intent.get("stop_policy") or "NONE").upper()
        lease = _iso(now + timedelta(seconds=lease_seconds)) if owner else None
        with self._tx():
            cur = self.db.execute(
                "INSERT OR IGNORE INTO orders(cid,day,status,payload,state,version,owner,lease_until,"
                "plan_id,trigger_id,intent,stop_policy,stop_status,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    cid, day, "unknown", _json({"intent": intent}), OrderState.RESERVED.value, 1,
                    owner, lease, plan_id, trigger_id, _json(intent),
                    stop_policy, "NONE_BY_AI" if stop_policy == "NONE" else "REQUESTED",
                    _iso(now), _iso(now),
                ),
            )
            created = bool(cur.rowcount)
            if created:
                self.db.execute(
                    "INSERT INTO order_requests(cid,cycle,ts,payload) VALUES(?,?,?,?)",
                    (cid, None, _iso(now), _json(intent)),
                )
                self.db.execute(
                    "INSERT INTO order_transitions(cid,ts,from_state,to_state,owner,reason)"
                    " VALUES(?,?,?,?,?,?)",
                    (cid, _iso(now), "", OrderState.RESERVED.value, owner, "reserved"),
                )
        return self.order_row(cid) or {}

    # Compatibility with the previous store API used by older callers/tests.
    def reserve(self, cid: str, day: str, payload: dict, cycle: str | None = None) -> bool:
        row = self.order_row(cid)
        if row:
            return False
        self.reserve_order(cid, day, payload, plan_id=None, trigger_id=None, owner=None)
        if cycle is not None:
            with self._tx():
                self.db.execute("UPDATE order_requests SET cycle=? WHERE cid=?", (cycle, cid))
        return True

    def order_row(self, cid: str) -> dict | None:
        with self.lock:
            row = self.db.execute("SELECT * FROM orders WHERE cid=?", (cid,)).fetchone()
        return dict(row) if row else None

    def claim_order(self, cid: str, owner: str, *, lease_seconds: int = 90) -> bool:
        now = self.clock()
        with self._tx():
            row = self.db.execute(
                "SELECT state,version,owner,lease_until FROM orders WHERE cid=?", (cid,)
            ).fetchone()
            if not row or row["state"] != OrderState.RESERVED.value:
                return False
            lease = _parse_iso(row["lease_until"])
            if lease and lease > now and row["owner"] not in (None, owner):
                return False
            cur = self.db.execute(
                "UPDATE orders SET state=?,owner=?,lease_until=?,version=version+1,updated_at=?"
                " WHERE cid=? AND state=? AND version=?",
                (
                    OrderState.VALIDATING.value, owner,
                    _iso(now + timedelta(seconds=lease_seconds)), _iso(now),
                    cid, OrderState.RESERVED.value, row["version"],
                ),
            )
            if cur.rowcount != 1:
                return False
            self.db.execute(
                "INSERT INTO order_transitions(cid,ts,from_state,to_state,owner,reason)"
                " VALUES(?,?,?,?,?,?)",
                (cid, _iso(now), OrderState.RESERVED.value, OrderState.VALIDATING.value, owner, "submit"),
            )
        return True

    def transition(
        self,
        cid: str,
        target: OrderState | str,
        *,
        expected: OrderState | str | Iterable[OrderState | str] | None = None,
        owner: str | None = None,
        reason: str | None = None,
        lease_seconds: int | None = None,
        fields: dict | None = None,
    ) -> bool:
        target = OrderState(target)
        now = self.clock()
        if expected is None:
            expected_set = None
        elif isinstance(expected, (str, OrderState)):
            expected_set = {OrderState(expected)}
        else:
            expected_set = {OrderState(item) for item in expected}
        with self._tx():
            row = self.db.execute(
                "SELECT state,version,owner FROM orders WHERE cid=?", (cid,)
            ).fetchone()
            if not row:
                return False
            current = OrderState(row["state"])
            if expected_set is not None and current not in expected_set:
                return False
            if owner is not None and row["owner"] not in (None, owner):
                return False
            if not can_transition(current, target):
                return False
            lease = None
            if lease_seconds is not None:
                lease = _iso(now + timedelta(seconds=lease_seconds))
            assignments = ["state=?", "version=version+1", "updated_at=?"]
            values: list[Any] = [target.value, _iso(now)]
            if lease is not None:
                assignments.append("lease_until=?")
                values.append(lease)
            if owner is not None:
                assignments.append("owner=?")
                values.append(owner)
            if reason is not None:
                assignments.append("reason=?")
                values.append(redact(reason))
            if target in TERMINAL_STATES:
                assignments.append("lease_until=NULL")
            if target is OrderState.NOT_SENT:
                assignments.append("not_sent_at=?")
                values.append(_iso(now))
            for key, value in (fields or {}).items():
                if key not in {
                    "broker_order_id", "stop_policy", "stop_status", "stop_order_id",
                    "dispatch_intent_at", "payload", "status", "plan_id", "trigger_id",
                }:
                    raise ValueError(f"Unsupported order field: {key}")
                assignments.append(f"{key}=?")
                values.append(_json(value) if key == "payload" else value)
            values.extend([cid, row["version"], current.value])
            cur = self.db.execute(
                f"UPDATE orders SET {','.join(assignments)} "
                "WHERE cid=? AND version=? AND state=?",
                tuple(values),
            )
            if cur.rowcount != 1:
                return False
            self.db.execute(
                "INSERT INTO order_transitions(cid,ts,from_state,to_state,owner,reason)"
                " VALUES(?,?,?,?,?,?)",
                (cid, _iso(now), current.value, target.value, owner, redact(reason or "")),
            )
        return True

    def mark_not_sent(self, cid: str, owner: str, reason: str) -> bool:
        """Resolve a pre-dispatch failure; no broker lookup is allowed here."""
        row = self.order_row(cid)
        if not row or OrderState(row["state"]) not in PRE_DISPATCH_STATES:
            return False
        return self.transition(
            cid, OrderState.NOT_SENT, expected=tuple(PRE_DISPATCH_STATES),
            owner=owner, reason=reason,
        )

    def record_dispatch_intent(self, cid: str, owner: str) -> bool:
        """Atomically cross the durable dispatch boundary.

        The dispatch marker is written in the same transaction as the state
        transition; a missing marker therefore proves no POST was attempted.
        """
        now = self.clock()
        with self._tx():
            row = self.db.execute(
                "SELECT state,version,owner FROM orders WHERE cid=?", (cid,)
            ).fetchone()
            if not row:
                return False
            if OrderState(row["state"]) != OrderState.READY_TO_DISPATCH:
                return False
            if owner is not None and row["owner"] not in (None, owner):
                return False
            cur = self.db.execute(
                "UPDATE orders SET state=?,version=version+1,dispatch_intent_at=?,"
                "lease_until=NULL,updated_at=? WHERE cid=? AND version=? AND state=?",
                (
                    OrderState.DISPATCH_INTENT_RECORDED.value, _iso(now), _iso(now),
                    cid, row["version"], OrderState.READY_TO_DISPATCH.value,
                ),
            )
            if cur.rowcount != 1:
                return False
            self.db.execute(
                "INSERT OR IGNORE INTO order_dispatches(cid,ts) VALUES(?,?)",
                (cid, _iso(now)),
            )
            self.db.execute(
                "INSERT INTO order_transitions(cid,ts,from_state,to_state,owner,reason)"
                " VALUES(?,?,?,?,?,?)",
                (cid, _iso(now), OrderState.READY_TO_DISPATCH.value,
                 OrderState.DISPATCH_INTENT_RECORDED.value, owner, "dispatch_intent"),
            )
        return True

    def dispatched(self, cid: str) -> bool:
        with self.lock:
            return bool(
                self.db.execute(
                    "SELECT 1 FROM order_dispatches WHERE cid=?", (cid,)
                ).fetchone()
            )

    def apply_broker_order(self, cid: str, order: dict) -> OrderState | None:
        order = dict(order)
        status = str(order.get("status") or "unknown").lower()
        target = broker_state(status)
        filled = str(order.get("filled_qty") or 0)
        broker_id = order.get("id")
        now = self.clock()
        conflict = False
        with self._tx():
            row = self.db.execute("SELECT * FROM orders WHERE cid=?", (cid,)).fetchone()
            if not row:
                return None
            current = OrderState(row["state"])
            previous_payload = _loads(row["payload"]) or {}
            try:
                from decimal import Decimal

                filled_dec = Decimal(filled)
                old_filled = Decimal(str(previous_payload.get("filled_qty") or 0))
            except Exception:
                filled_dec, old_filled = None, None
            if old_filled is not None and filled_dec is not None and filled_dec < old_filled:
                # Out-of-order polls never erase a fill.
                return current
            target_state = target
            if current in TERMINAL_STATES:
                if target in TERMINAL_STATES and target != current:
                    conflict = True
                elif target not in TERMINAL_STATES:
                    target_state = OrderState.RECONCILIATION_REQUIRED
                    conflict = True
            elif current == OrderState.RECONCILIATION_REQUIRED:
                # Broker facts may resolve an ambiguous local state.
                target_state = target
            elif current != target and not can_transition(current, target):
                target_state = OrderState.RECONCILIATION_REQUIRED
                conflict = True
            payload = dict(order)
            payload["status"] = status
            assignments = [
                "state=?", "status=?", "payload=?", "version=version+1", "updated_at=?"
            ]
            values: list[Any] = [target_state.value, status, _json(payload), _iso(now)]
            if broker_id:
                assignments.append("broker_order_id=?")
                values.append(str(broker_id))
            if target_state in TERMINAL_STATES:
                assignments.append("lease_until=NULL")
            # Stop leg facts: Alpaca OTO/bracket orders expose legs.
            legs = order.get("legs") or []
            stop_legs = [
                leg for leg in legs
                if str(leg.get("type") or "").lower() in {"stop", "stop_limit"}
                or str(leg.get("order_type") or "").lower() in {"stop", "stop_limit"}
            ]
            if stop_legs:
                leg = stop_legs[0]
                assignments.append("stop_order_id=?")
                values.append(str(leg.get("id") or ""))
                assignments.append("stop_status=?")
                values.append("VERIFIED" if str(leg.get("status") or "").lower()
                              in {"new", "accepted", "held"} else "UNKNOWN")
            elif row["stop_policy"] == "BROKER_STOP" and target_state in {
                OrderState.PARTIALLY_FILLED, OrderState.FILLED,
            }:
                # A requested broker stop has no verified leg on a filled or
                # partially-filled parent, even if some other leg exists.
                assignments.append("stop_status=?")
                values.append("FAILED")
                conflict = True
            values.extend([cid, row["version"]])
            cur = self.db.execute(
                f"UPDATE orders SET {','.join(assignments)} WHERE cid=? AND version=?",
                tuple(values),
            )
            if cur.rowcount != 1:
                return current
            self.db.execute(
                "INSERT INTO order_transitions(cid,ts,from_state,to_state,owner,reason)"
                " VALUES(?,?,?,?,?,?)",
                (cid, _iso(now), current.value, target_state.value, row["owner"],
                 "broker:" + status + (";conflict" if conflict else "")),
            )
            if filled_dec is not None and filled_dec > 0:
                self.db.execute(
                    "INSERT OR IGNORE INTO fills(cid,cumulative_qty,ts,payload) VALUES(?,?,?,?)",
                    (cid, str(filled_dec.normalize()), _iso(now), _json(payload)),
                )
        self.event("order", {**order, "cid": cid, "state": target_state.value})
        if conflict:
            self.block(f"Local/broker order state conflict; reconciliation required: {cid}")
        return target_state

    # Old API name retained for callers/tests.
    def order(self, cid: str, order: dict) -> OrderState | None:
        return self.apply_broker_order(cid, order)

    def uncertain(self, cid: str, reason: str) -> None:
        self.transition(
            cid, OrderState.SUBMISSION_OUTCOME_UNKNOWN,
            expected=(OrderState.DISPATCH_INTENT_RECORDED, OrderState.SUBMISSION_OUTCOME_UNKNOWN),
            reason=reason,
        )
        self.block(reason)

    def pending(self) -> list[str]:
        return [row["cid"] for row in self.unresolved_orders()]

    def unresolved_orders(self) -> list[dict]:
        states = [s.value for s in OrderState if s not in TERMINAL_STATES]
        placeholders = ",".join("?" for _ in states)
        with self.lock:
            rows = self.db.execute(
                f"SELECT * FROM orders WHERE state IN ({placeholders}) ORDER BY created_at, cid",
                tuple(states),
            ).fetchall()
        return [dict(row) for row in rows]

    def orders_for_reconcile(self) -> list[dict]:
        from .order_state import POST_DISPATCH_STATES

        states = [s.value for s in POST_DISPATCH_STATES]
        placeholders = ",".join("?" for _ in states)
        with self.lock:
            rows = self.db.execute(
                f"SELECT * FROM orders WHERE state IN ({placeholders}) ORDER BY created_at, cid",
                tuple(states),
            ).fetchall()
        return [dict(row) for row in rows]

    def import_broker_order(self, cid: str, order: dict) -> None:
        """Import a broker order found at Alpaca but absent locally.

        We cannot prove when/how it was dispatched, so it is imported as an
        unresolved fact and new risk is frozen until reconciliation.
        """
        now = self.clock()
        payload = dict(order)
        payload["status"] = str(order.get("status") or "unknown")
        day = str(order.get("created_at") or now.date().isoformat())[:10]
        with self._tx():
            self.db.execute(
                "INSERT OR IGNORE INTO orders(cid,day,status,payload,state,version,owner,"
                "lease_until,intent,broker_order_id,created_at,updated_at,reason) "
                "VALUES(?,?,?,?,?,1,NULL,NULL,NULL,?,?,?,?)",
                (
                    cid, day, payload["status"], _json(payload),
                    OrderState.RECONCILIATION_REQUIRED.value,
                    str(order.get("id") or ""), _iso(now), _iso(now),
                    "recovered broker order without local dispatch proof",
                ),
            )
            self.db.execute(
                "INSERT INTO order_transitions(cid,ts,from_state,to_state,owner,reason)"
                " VALUES(?,?,?,?,?,?)",
                (cid, _iso(now), "", OrderState.RECONCILIATION_REQUIRED.value,
                 self.instance_id, "broker_orphan_import"),
            )
        self.event("order_imported", {"cid": cid, "outcome": "reconciliation_required"})
        self.block("Recovered broker order without local dispatch proof: " + cid)

    def recover_stale_leases(self) -> list[str]:
        """Resolve local-only leases that expired before the dispatch boundary.

        These orders cannot have reached the broker, so they are marked
        ``NOT_SENT`` without a lookup.  Post-dispatch states are deliberately
        never resolved here.
        """
        now = self.clock()
        recovered = []
        with self._tx():
            rows = self.db.execute(
                "SELECT cid,state,owner,lease_until FROM orders WHERE state IN (?,?,?)",
                (
                    OrderState.RESERVED.value,
                    OrderState.VALIDATING.value,
                    OrderState.READY_TO_DISPATCH.value,
                ),
            ).fetchall()
            for row in rows:
                lease = _parse_iso(row["lease_until"])
                owner_instance = str(row["owner"] or "").split(":", 1)[0]
                stale = (
                    lease is None
                    or lease <= now
                    or owner_instance != self.instance_id
                )
                if not stale:
                    continue
                cur = self.db.execute(
                    "UPDATE orders SET state=?,not_sent_at=?,reason=?,version=version+1,"
                    "lease_until=NULL,updated_at=? WHERE cid=? AND state=?",
                    (
                        OrderState.NOT_SENT.value, _iso(now),
                        "stale local lease before dispatch; no POST possible",
                        _iso(now), row["cid"], row["state"],
                    ),
                )
                if cur.rowcount:
                    recovered.append(row["cid"])
                    self.db.execute(
                        "INSERT INTO order_transitions(cid,ts,from_state,to_state,owner,reason)"
                        " VALUES(?,?,?,?,?,?)",
                        (row["cid"], _iso(now), row["state"], OrderState.NOT_SENT.value,
                         self.instance_id, "stale_lease_recovery"),
                    )
        return recovered

    def request(self, cid: str) -> dict | None:
        with self.lock:
            row = self.db.execute(
                "SELECT cycle,payload FROM order_requests WHERE cid=?", (cid,)
            ).fetchone()
        return {"cycle": row[0], "request": _loads(row[1])} if row else None

    def count(self, day: str) -> int:
        with self.lock:
            return int(
                self.db.execute(
                    "SELECT count(*) FROM orders WHERE day=?", (day,)
                ).fetchone()[0]
            )

    def count_orders(self, day: str, *, side: str | None = None) -> int:
        rows = self.orders_for_day(day)
        if side:
            rows = [r for r in rows if (_loads(r.get("intent")) or {}).get("side") == side]
        return len(rows)

    def count_buy_attempts(self, day: str) -> int:
        rows = self.orders_for_day(day)
        count = 0
        for row in rows:
            intent = _loads(row.get("intent")) or {}
            if str(intent.get("side") or intent.get("action") or "").lower() == "buy":
                count += 1
        return count

    def count_order_attempts(self, day: str) -> int:
        """All submitted order attempts, including short opens and covers."""
        return len(self.orders_for_day(day))

    def has_unknown_orders(self) -> bool:
        with self.lock:
            row = self.db.execute(
                "SELECT 1 FROM orders WHERE state IN (?,?) LIMIT 1",
                (OrderState.SUBMISSION_OUTCOME_UNKNOWN.value,
                 OrderState.RECONCILIATION_REQUIRED.value),
            ).fetchone()
        return bool(row)

    def resolve_unknown_not_sent(self, cid: str, reason: str) -> bool:
        """Operator-attested recovery for a post-dispatch unknown order.

        This is deliberately explicit and audited: it is not automatic and is
        never used for an order with a broker record.  The operator is expected
        to have searched Alpaca by client_order_id first.
        """
        with self._tx():
            row = self.db.execute(
                "SELECT state,version FROM orders WHERE cid=?", (cid,)
            ).fetchone()
            if not row:
                return False
            current = OrderState(row["state"])
            if current not in {
                OrderState.DISPATCH_INTENT_RECORDED, OrderState.SUBMISSION_OUTCOME_UNKNOWN,
            }:
                return False
            cur = self.db.execute(
                "UPDATE orders SET state=?,not_sent_at=?,reason=?,version=version+1,"
                "lease_until=NULL,updated_at=? WHERE cid=? AND version=?",
                (
                    OrderState.NOT_SENT.value, _iso(self.clock()),
                    "operator-attested not sent: " + redact(reason),
                    _iso(self.clock()), cid, row["version"],
                ),
            )
            if not cur.rowcount:
                return False
            self.db.execute(
                "INSERT INTO order_transitions(cid,ts,from_state,to_state,owner,reason)"
                " VALUES(?,?,?,?,?,?)",
                (cid, _iso(self.clock()), current.value, OrderState.NOT_SENT.value,
                 self.instance_id, "operator_resolution: " + redact(reason)),
            )
        self.event("operator_resolution", {
            "cid": cid, "resolution": "NOT_SENT", "reason": reason,
        })
        return True

    def freeze_reason(self) -> str:
        if self.has_unknown_orders():
            return "ORDER_UNKNOWN_OR_CONFLICT"
        return self.safety_reason()

    def latest_buy_intent(self, symbol: str) -> dict | None:
        with self.lock:
            rows = self.db.execute(
                "SELECT intent,state FROM orders ORDER BY rowid DESC"
            ).fetchall()
        for row in rows:
            intent = _loads(row["intent"]) or {}
            if str(intent.get("symbol") or "") != symbol:
                continue
            if str(intent.get("side") or intent.get("action") or "").lower() != "buy":
                continue
            if OrderState(row["state"]) is OrderState.FILLED:
                return intent
        return None

    def latest_intent(self, symbol: str) -> dict | None:
        """Latest filled intent for either direction (short stop/status view)."""
        with self.lock:
            rows = self.db.execute(
                "SELECT intent,state FROM orders ORDER BY rowid DESC"
            ).fetchall()
        for row in rows:
            intent = _loads(row["intent"]) or {}
            if str(intent.get("symbol") or "") != symbol:
                continue
            if OrderState(row["state"]) is OrderState.FILLED:
                return intent
        return None

    def ai_calls_for_day(self, day: str, mode: str | None = None) -> list[dict]:
        start = day + "T00:00:00"
        end = day + "T23:59:59.999999"
        sql = "SELECT * FROM ai_calls WHERE ts>=? AND ts<=?"
        params: list = [start, end]
        if mode:
            sql += " AND mode=?"
            params.append(mode)
        sql += " ORDER BY id"
        with self.lock:
            rows = self.db.execute(sql, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def set_agent_call_cost(self, run_id: str, role: str, cost_usd: float | None) -> None:
        if cost_usd is None:
            return
        with self._tx():
            self.db.execute(
                "UPDATE ai_calls SET cost_usd=? WHERE id=("
                "SELECT id FROM ai_calls WHERE run_id=? AND agent_role=? "
                "ORDER BY id DESC LIMIT 1)",
                (float(cost_usd), run_id, role),
            )

    # --------------------------------------------------------- multi-agent audit
    def create_agent_run(self, run: dict) -> None:
        now = _iso(self.clock())
        with self._tx():
            self.db.execute(
                "INSERT OR IGNORE INTO agent_runs(run_id,trading_day,mode,status,as_of_utc,"
                "universe_version,config_hash,snapshot_hash,baseline_status,error,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run["run_id"], run.get("trading_day"), run.get("mode", "THREE_AGENT"),
                    run.get("status", "RUNNING"), run.get("as_of_utc"),
                    run.get("universe_version"), run.get("config_hash"),
                    run.get("snapshot_hash"), run.get("baseline_status", "PENDING"),
                    run.get("error", ""), now, now,
                ),
            )
            self.db.execute(
                "UPDATE agent_runs SET trading_day=?,mode=?,status=?,as_of_utc=?,"
                "universe_version=?,config_hash=?,snapshot_hash=?,baseline_status=?,error=?,"
                "updated_at=? WHERE run_id=?",
                (
                    run.get("trading_day"), run.get("mode", "THREE_AGENT"),
                    run.get("status", "RUNNING"), run.get("as_of_utc"),
                    run.get("universe_version"), run.get("config_hash"),
                    run.get("snapshot_hash"), run.get("baseline_status", "PENDING"),
                    run.get("error", ""), now, run["run_id"],
                ),
            )

    def update_agent_run(self, run_id: str, **fields) -> None:
        allowed = {
            "status", "baseline_status", "error", "snapshot_hash", "universe_version",
        }
        if not fields or any(key not in allowed for key in fields):
            raise ValueError("Unsupported agent run field")
        assignments = ", ".join(f"{key}=?" for key in fields) + ", updated_at=?"
        with self._tx():
            self.db.execute(
                f"UPDATE agent_runs SET {assignments} WHERE run_id=?",
                (*fields.values(), _iso(self.clock()), run_id),
            )

    def agent_run(self, run_id: str) -> dict | None:
        with self.lock:
            row = self.db.execute("SELECT * FROM agent_runs WHERE run_id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def recent_agent_runs(self, limit: int = 10) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM agent_runs ORDER BY created_at DESC, run_id DESC LIMIT ?",
                (max(1, min(int(limit), 50)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def save_agent_report(self, report: dict) -> None:
        now = _iso(self.clock())
        with self._tx():
            self.db.execute(
                "INSERT INTO agent_reports(report_id,run_id,role,symbol,status,schema_version,"
                "model,prompt_hash,input_hash,output_hash,report_json,parsed_json,error,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(report_id) DO UPDATE SET status=excluded.status,"
                " model=excluded.model,prompt_hash=excluded.prompt_hash,input_hash=excluded.input_hash,"
                " output_hash=excluded.output_hash,report_json=excluded.report_json,"
                " parsed_json=excluded.parsed_json,error=excluded.error,updated_at=excluded.updated_at",
                (
                    report["report_id"], report["run_id"], report["role"],
                    report.get("symbol", ""), report.get("status", "OK"),
                    report.get("schema_version", "1.0"), report.get("model"),
                    report.get("prompt_hash"), report.get("input_hash"),
                    report.get("output_hash"), report.get("report_json"),
                    _json(report.get("parsed_json") or {}), report.get("error", ""),
                    now, now,
                ),
            )

    def agent_report(self, report_id: str) -> dict | None:
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM agent_reports WHERE report_id=?", (report_id,)
            ).fetchone()
        return dict(row) if row else None

    def agent_reports(self, *, run_id: str | None = None, symbol: str | None = None,
                      limit: int = 100) -> list[dict]:
        sql = "SELECT * FROM agent_reports"
        clauses = []
        params: list = []
        if run_id:
            clauses.append("run_id=?")
            params.append(run_id)
        if symbol:
            clauses.append("symbol=?")
            params.append(symbol)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at, report_id LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        with self.lock:
            rows = self.db.execute(sql, tuple(params)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["parsed"] = _loads(item.pop("parsed_json", None)) or {}
            result.append(item)
        return result

    def agent_memory(self, symbols: Iterable[str], per_symbol: int = 2) -> dict[str, list[dict]]:
        memory: dict[str, list[dict]] = {}
        with self.lock:
            for symbol in symbols:
                rows = self.db.execute(
                    "SELECT report_id,run_id,role,status,parsed_json,error,created_at "
                    "FROM agent_reports WHERE symbol=? AND status='OK' "
                    "ORDER BY created_at DESC, report_id DESC LIMIT ?",
                    (symbol, max(1, min(int(per_symbol), 10))),
                ).fetchall()
                items = []
                for row in rows:
                    parsed = _loads(row["parsed_json"]) or {}
                    items.append({
                        "report_id": row["report_id"], "run_id": row["run_id"],
                        "role": row["role"], "created_at": row["created_at"],
                        "summary": str(
                            parsed.get("research_conclusion")
                            or parsed.get("overall_assessment")
                            or parsed.get("market_decision")
                            or ""
                        )[:240],
                    })
                if items:
                    memory[symbol] = items
        return memory

    def link_agent_plan(self, run_id: str, plan_id: str, *, report_id: str,
                        role: str = "portfolio_manager", executed: bool = False) -> None:
        with self._tx():
            self.db.execute(
                "INSERT OR REPLACE INTO agent_plan_links(run_id,plan_id,report_id,role,executed,created_at)"
                " VALUES(?,?,?,?,?,?)",
                (run_id, plan_id, report_id, role, 1 if executed else 0, _iso(self.clock())),
            )

    def agent_plan_links(self, run_id: str) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM agent_plan_links WHERE run_id=? ORDER BY created_at", (run_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_agent_plan_executed(self, plan_id: str) -> None:
        with self._tx():
            self.db.execute(
                "UPDATE agent_plan_links SET executed=1 WHERE plan_id=?", (plan_id,)
            )

    def record_agent_feedback(self, *, run_id: str, symbol: str, plan_id: str,
                              outcome: str, payload: dict | None = None) -> None:
        now = _iso(self.clock())
        with self._tx():
            self.db.execute(
                "INSERT INTO agent_feedback(run_id,symbol,plan_id,observed_at,outcome,payload,created_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (run_id, symbol, plan_id, now, outcome, _json(redact(payload or {})), now),
            )

    def agent_feedback(self, symbol: str | None = None, limit: int = 20) -> list[dict]:
        sql = "SELECT * FROM agent_feedback"
        params: list = []
        if symbol:
            sql += " WHERE symbol=?"
            params.append(symbol)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(int(limit), 200)))
        with self.lock:
            rows = self.db.execute(sql, tuple(params)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = _loads(item.get("payload")) or {}
            result.append(item)
        return result

    def agent_cache_get(self, cache_key: str) -> dict | None:
        now = _iso(self.clock())
        with self._tx():
            row = self.db.execute(
                "SELECT * FROM agent_cache WHERE cache_key=? AND expires_at>?",
                (cache_key, now),
            ).fetchone()
            if row:
                self.db.execute(
                    "UPDATE agent_cache SET hits=hits+1 WHERE cache_key=?", (cache_key,)
                )
        if not row:
            return None
        item = dict(row)
        item["output"] = _loads(item.get("output_json")) or {}
        return item

    def agent_cache_put(self, *, cache_key: str, role: str, model: str,
                        input_hash: str, output: dict, ttl_seconds: int = 900) -> None:
        now = self.clock()
        with self._tx():
            self.db.execute(
                "INSERT OR REPLACE INTO agent_cache(cache_key,role,model,input_hash,output_json,"
                "created_at,expires_at,hits) VALUES(?,?,?,?,?,?,?,0)",
                (
                    cache_key, role, model, input_hash, _json(output),
                    _iso(now), _iso(now + timedelta(seconds=max(1, int(ttl_seconds)))),
                ),
            )

    def agent_usage_for_day(self, day: str) -> list[dict]:
        start = day + "T00:00:00"
        end = day + "T23:59:59.999999"
        with self.lock:
            rows = self.db.execute(
                "SELECT COALESCE(agent_role,'single_agent') AS role, "
                "COUNT(*) AS calls, "
                "COALESCE(SUM(prompt_tokens),0) AS prompt_tokens, "
                "COALESCE(SUM(completion_tokens),0) AS completion_tokens, "
                "COALESCE(SUM(latency_ms),0) AS latency_ms, "
                "COALESCE(SUM(cost_usd),0) AS cost_usd, "
                "COALESCE(SUM(cached),0) AS cached_calls "
                "FROM ai_calls WHERE ts>=? AND ts<=? GROUP BY COALESCE(agent_role,'single_agent') "
                "ORDER BY role",
                (start, end),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_agent_experiment(self, experiment: dict) -> None:
        with self._tx():
            self.db.execute(
                "INSERT OR REPLACE INTO agent_experiments(run_id,trading_day,snapshot_hash,"
                "baseline_report_id,three_agent_plan_id,baseline_decision,three_agent_decision,"
                "baseline_tokens,three_agent_tokens,baseline_latency_ms,three_agent_latency_ms,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    experiment["run_id"], experiment.get("trading_day"),
                    experiment.get("snapshot_hash"), experiment.get("baseline_report_id"),
                    experiment.get("three_agent_plan_id"), _json(experiment.get("baseline_decision") or {}),
                    _json(experiment.get("three_agent_decision") or {}),
                    experiment.get("baseline_tokens"), experiment.get("three_agent_tokens"),
                    experiment.get("baseline_latency_ms"), experiment.get("three_agent_latency_ms"),
                    _iso(self.clock()),
                ),
            )

    def agent_experiments(self, limit: int = 20) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM agent_experiments ORDER BY created_at DESC LIMIT ?",
                (max(1, min(int(limit), 100)),),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["baseline_decision"] = _loads(item.get("baseline_decision")) or {}
            item["three_agent_decision"] = _loads(item.get("three_agent_decision")) or {}
            result.append(item)
        return result

    def orders_for_day(self, day: str) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM orders WHERE day=? ORDER BY created_at, cid", (day,)
            ).fetchall()
        return [dict(row) for row in rows]

    # ---------------------------------------------------------------- plans/AI
    def save_plan(self, plan: dict, *, config_hash: str, trading_day: str, supersede: bool = True) -> None:
        plan_id = plan["plan_id"]
        valid_until = plan.get("plan_valid_until_utc") or plan.get("valid_until")
        now = _iso(self.clock())
        with self._tx():
            if supersede:
                self.db.execute(
                    "UPDATE plans SET status='SUPERSEDED', superseded_by=?, reason='new plan' "
                    "WHERE trading_day=? AND status IN ('ACTIVE','WATCHING','TRIGGERED')",
                    (plan_id, trading_day),
                )
            self.db.execute(
                "INSERT INTO plans(plan_id,trading_day,mode,status,version,payload,"
                "config_hash,created_at,valid_until) VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(plan_id) DO UPDATE SET status=excluded.status,"
                "payload=excluded.payload,valid_until=excluded.valid_until",
                (
                    plan_id, trading_day, plan.get("mode", "PREMARKET_PLAN"), "ACTIVE", 1,
                    _json(plan), config_hash, now, valid_until,
                ),
            )
            for candidate in plan.get("candidates", []):
                for trigger in candidate.get("triggers", []) or []:
                    self.db.execute(
                        "INSERT OR IGNORE INTO plan_triggers(plan_id,trigger_id,symbol,purpose,payload)"
                        " VALUES(?,?,?,?,?)",
                        (
                            plan_id, trigger["trigger_id"], candidate["symbol"],
                            trigger["purpose"], _json(trigger),
                        ),
                    )

    def supersede_plans(self, trading_day: str, *, reason: str, except_plan_id: str | None = None) -> None:
        with self._tx():
            if except_plan_id:
                self.db.execute(
                    "UPDATE plans SET status='SUPERSEDED', reason=? WHERE trading_day=? "
                    "AND status IN ('ACTIVE','WATCHING','TRIGGERED') AND plan_id<>?",
                    (reason, trading_day, except_plan_id),
                )
            else:
                self.db.execute(
                    "UPDATE plans SET status='SUPERSEDED', reason=? WHERE trading_day=? "
                    "AND status IN ('ACTIVE','WATCHING','TRIGGERED')",
                    (reason, trading_day),
                )

    def active_plans(self, trading_day: str) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM plans WHERE trading_day=? AND status IN ('ACTIVE','WATCHING','TRIGGERED')"
                " ORDER BY created_at, plan_id",
                (trading_day,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = _loads(row["payload"])
            result.append(item)
        return result

    def plan(self, plan_id: str) -> dict | None:
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM plans WHERE plan_id=?", (plan_id,)
            ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["payload"] = _loads(row["payload"])
        return result

    def recent_plans(self, limit: int = 100) -> list[dict]:
        """All plans, including superseded/invalidated history."""
        limit = max(1, min(int(limit), 500))
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM plans ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = _loads(row["payload"])
            result.append(item)
        return result

    def active_plan(self, trading_day: str) -> dict | None:
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM plans WHERE trading_day=? AND status IN ('ACTIVE','WATCHING','TRIGGERED')"
                " ORDER BY created_at DESC LIMIT 1",
                (trading_day,),
            ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["payload"] = _loads(row["payload"])
        return result

    def set_plan_status(self, plan_id: str, status: str, reason: str | None = None) -> None:
        self.set_control("plan_status_" + plan_id, status)
        with self._tx():
            self.db.execute(
                "UPDATE plans SET status=?,reason=COALESCE(?,reason) WHERE plan_id=?",
                (status, reason, plan_id),
            )
        self.event("plan", {"plan_id": plan_id, "status": status, "reason": reason})

    def mark_trigger_fired(self, plan_id: str, trigger_id: str, context_hash: str) -> bool:
        with self._tx():
            cur = self.db.execute(
                "UPDATE plan_triggers SET fired_at=?,context_hash=? "
                "WHERE plan_id=? AND trigger_id=? AND fired_at IS NULL",
                (_iso(self.clock()), context_hash, plan_id, trigger_id),
            )
            return bool(cur.rowcount)

    def trigger_fired(self, plan_id: str, trigger_id: str) -> bool:
        with self.lock:
            row = self.db.execute(
                "SELECT fired_at FROM plan_triggers WHERE plan_id=? AND trigger_id=?",
                (plan_id, trigger_id),
            ).fetchone()
        return bool(row and row["fired_at"])

    def rearm_trigger(self, plan_id: str, trigger_id: str) -> None:
        """Allow a retry only after an order was provably NOT_SENT."""
        with self._tx():
            self.db.execute(
                "UPDATE plan_triggers SET fired_at=NULL,context_hash=NULL "
                "WHERE plan_id=? AND trigger_id=?",
                (plan_id, trigger_id),
            )

    def trigger_attempts(self, plan_id: str, trigger_id: str) -> int:
        with self.lock:
            row = self.db.execute(
                "SELECT count(*) FROM orders WHERE plan_id=? AND trigger_id=?",
                (plan_id, trigger_id),
            ).fetchone()
        return int(row[0]) if row else 0

    def plan_trigger(self, plan_id: str, trigger_id: str) -> dict | None:
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM plan_triggers WHERE plan_id=? AND trigger_id=?",
                (plan_id, trigger_id),
            ).fetchone()
        return dict(row) if row else None

    def ai_call(self, record: dict) -> int:
        with self._tx():
            cur = self.db.execute(
                "INSERT INTO ai_calls(ts,mode,model,prompt_hash,input_hash,output_hash,"
                "prompt_tokens,completion_tokens,latency_ms,finish_reason,outcome,plan_id,error,usage,"
                "run_id,agent_role,cost_usd,cached)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    _iso(self.clock()), record.get("mode"), record.get("model"),
                    record.get("prompt_hash"), record.get("input_hash"), record.get("output_hash"),
                    record.get("prompt_tokens"), record.get("completion_tokens"),
                    record.get("latency_ms"), record.get("finish_reason"),
                    record.get("outcome"), record.get("plan_id"), record.get("error"),
                    _json(record.get("usage") or {}), record.get("run_id"),
                    record.get("agent_role"), record.get("cost_usd"),
                    1 if record.get("cached") else 0,
                ),
            )
            return int(cur.lastrowid)

    # --------------------------------------------------------------- sessions
    def claim_stage(
        self, session_id: str, trading_day: str, phase: str, *,
        stale_seconds: int = 600, max_attempts: int = 3,
    ) -> bool:
        now = self.clock()
        with self._tx():
            row = self.db.execute(
                "SELECT status,attempts,updated_at FROM sessions WHERE session_id=?",
                (session_id,),
            ).fetchone()
            if not row:
                self.db.execute(
                    "INSERT INTO sessions(session_id,trading_day,phase,status,attempts,updated_at)"
                    " VALUES(?,?,?,?,1,?)",
                    (session_id, trading_day, phase, "RUNNING", _iso(now)),
                )
                return True
            updated = _parse_iso(row["updated_at"])
            if row["status"] == "DONE":
                return False
            if row["status"] in {"FAILED", "RUNNING"} and updated and (
                now - updated
            ).total_seconds() < stale_seconds:
                return False
            if row["status"] in {"FAILED", "RUNNING"}:
                if int(row["attempts"] or 0) >= max_attempts:
                    return False
                self.db.execute(
                    "UPDATE sessions SET status='RUNNING',attempts=attempts+1,updated_at=?"
                    " WHERE session_id=?",
                    (_iso(now), session_id),
                )
                return True
            return False

    def finish_stage(self, session_id: str, status: str, checkpoint: dict | None = None) -> None:
        with self._tx():
            self.db.execute(
                "UPDATE sessions SET status=?,checkpoint=?,updated_at=? WHERE session_id=?",
                (status, _json(checkpoint or {}), _iso(self.clock()), session_id),
            )

    # --------------------------------------------------------------- research
    def save_research_profile(self, profile: dict, *, evidence: list[dict], revision: dict | None = None) -> None:
        now = _iso(self.clock())
        with self._tx():
            self.db.execute(
                "INSERT INTO research_profiles(symbol,version,status,profile,source_hash,"
                "created_at,verified_at,expires_at) VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(symbol) DO UPDATE SET version=excluded.version,status=excluded.status,"
                "profile=excluded.profile,source_hash=excluded.source_hash,"
                "created_at=excluded.created_at,verified_at=excluded.verified_at,"
                "expires_at=excluded.expires_at",
                (
                    profile["symbol"], profile.get("version", 1), profile.get("status", "UNKNOWN"),
                    _json(profile), profile.get("source_hash"), now,
                    profile.get("verified_at_utc"), profile.get("expires_at_utc"),
                ),
            )
            for item in evidence:
                self.db.execute(
                    "INSERT INTO research_evidence(symbol,kind,source_url,title,published_at,"
                    "retrieved_at,available_at,verified,content_hash,summary,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        profile["symbol"], item.get("kind"), item.get("source_url"),
                        item.get("title"), item.get("published_at_utc"),
                        item.get("retrieved_at_utc"), item.get("available_at_utc"),
                        1 if item.get("verified") else 0, item.get("content_hash"),
                        item.get("summary"), now,
                    ),
                )
            if revision:
                self.db.execute(
                    "INSERT INTO research_revisions(symbol,version,previous_hash,new_hash,reason,"
                    "created_at,payload) VALUES(?,?,?,?,?,?,?)",
                    (
                        profile["symbol"], profile.get("version", 1), revision.get("previous_hash"),
                        revision.get("new_hash"), revision.get("reason"), now, _json(revision.get("payload") or {}),
                    ),
                )

    def research_profile(self, symbol: str) -> dict | None:
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM research_profiles WHERE symbol=?", (symbol,)
            ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["profile"] = _loads(row["profile"])
        return result

    def research_evidence(self, symbol: str, limit: int = 50) -> list[dict]:
        limit = max(1, min(int(limit), 200))
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM research_evidence WHERE symbol=? ORDER BY id DESC LIMIT ?",
                (symbol, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def research_summaries(self, symbols: Iterable[str]) -> dict[str, dict]:
        result = {}
        for symbol in symbols:
            row = self.research_profile(symbol)
            if row:
                result[symbol] = {
                    "symbol": symbol,
                    "status": row["status"],
                    "version": row["version"],
                    "verified_at_utc": row["verified_at"],
                    "expires_at_utc": row["expires_at"],
                    "profile": row["profile"],
                }
        return result

    def save_daily_scan(self, trading_day: str, status: str, candidates: list, profile_hash: str, ai_call_id: int | None = None) -> None:
        with self._tx():
            self.db.execute(
                "INSERT INTO daily_scans(trading_day,status,candidates,profile_hash,created_at,ai_call_id)"
                " VALUES(?,?,?,?,?,?) ON CONFLICT(trading_day) DO UPDATE SET status=excluded.status,"
                "candidates=excluded.candidates,profile_hash=excluded.profile_hash,"
                "created_at=excluded.created_at,ai_call_id=excluded.ai_call_id",
                (trading_day, status, _json(candidates), profile_hash, _iso(self.clock()), ai_call_id),
            )

    def daily_scan(self, trading_day: str) -> dict | None:
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM daily_scans WHERE trading_day=?", (trading_day,)
            ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["candidates"] = _loads(row["candidates"])
        return result
