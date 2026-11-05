import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from .safety import redact

TERMINAL = {"filled", "canceled", "expired", "rejected"}
BROKER_STATES = TERMINAL | {"accepted", "pending_new", "new", "partially_filled", "pending_cancel",
                           "done_for_day", "replaced", "pending_replace", "suspended", "calculated", "accepted_for_bidding", "stopped"}


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = RLock()
        self.db = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, ts TEXT, kind TEXT, payload TEXT);
        CREATE TABLE IF NOT EXISTS cycles(id TEXT PRIMARY KEY, status TEXT);
        CREATE TABLE IF NOT EXISTS orders(cid TEXT PRIMARY KEY, day TEXT, status TEXT, payload TEXT);
        CREATE TABLE IF NOT EXISTS controls(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS order_requests(cid TEXT PRIMARY KEY, cycle TEXT, ts TEXT, payload TEXT);
        CREATE TABLE IF NOT EXISTS fills(cid TEXT, cumulative_qty TEXT, ts TEXT, payload TEXT,
                                         PRIMARY KEY(cid, cumulative_qty));
        CREATE TABLE IF NOT EXISTS order_dispatches(cid TEXT PRIMARY KEY, ts TEXT);
        ''')
        self.db.commit()

    def event(self, kind, payload):
        with self.lock:
            self.db.execute("INSERT INTO events(ts,kind,payload) VALUES(?,?,?)", (
                datetime.now(timezone.utc).isoformat(), kind, json.dumps(redact(payload), default=str, allow_nan=False)))
            self.db.commit()

    def error(self, stage, exc, *, cycle=None, ticker=None, cid=None, outcome="blocked", retried=False):
        import requests
        network=isinstance(exc,(requests.RequestException,TimeoutError,ConnectionError))
        category=("deepseek_request" if stage=="deepseek_request" else
                  "ai_format" if stage=="deepseek_schema" else
                  "order_cancel" if stage=="order_cancel" else
                  "order_unknown" if stage in {"order_unknown","order_reconciliation"} else
                  "order_submit" if stage=="order_submit" else
                  "alpaca_network" if network else
                  "market_data" if stage in {"intraday_data","daily_data","quote"} else stage)
        payload = {"stage": stage, "type": type(exc).__name__, "reason": str(exc), "cycle": cycle,
                   "ticker": ticker, "cid": cid, "retried": retried, "outcome": outcome,"category":category}
        self.event("error", payload)
        logging.error("%s", json.dumps(redact(payload), ensure_ascii=False))

    def claim(self, cycle):
        with self.lock:
            cursor = self.db.execute("INSERT OR IGNORE INTO cycles VALUES(?, 'started')", (cycle,))
            self.db.commit()
            return bool(cursor.rowcount)

    def finish(self, cycle, status):
        with self.lock:
            self.db.execute("UPDATE cycles SET status=? WHERE id=?", (status, cycle))
            self.db.commit()

    def reserve(self, cid, day, payload, cycle=None):
        with self.lock, self.db:
            cursor = self.db.execute("INSERT OR IGNORE INTO orders VALUES(?,?,?,?)", (cid, day, "unknown", json.dumps(redact(payload))))
            if cursor.rowcount:
                self.db.execute("INSERT INTO order_requests VALUES(?,?,?,?)", (
                    cid, cycle, datetime.now(timezone.utc).isoformat(), json.dumps(redact(payload))))
            return bool(cursor.rowcount)

    def request(self, cid):
        with self.lock:
            row = self.db.execute("SELECT cycle,payload FROM order_requests WHERE cid=?", (cid,)).fetchone()
        return {"cycle": row[0], "request": json.loads(row[1])} if row else None

    def dispatch_once(self, cid):
        with self.lock, self.db:
            cursor=self.db.execute("INSERT OR IGNORE INTO order_dispatches VALUES(?,?)",(cid,datetime.now(timezone.utc).isoformat()))
            return bool(cursor.rowcount)

    def order(self, cid, order):
        from decimal import Decimal
        order = redact(dict(order))
        status = str(order.get("status", "unknown"))
        if status not in BROKER_STATES:
            order["broker_status"] = status
            status = "reconciliation_required"
        order["status"] = status
        context = self.request(cid) or {}
        order.update(cid=cid, cycle=context.get("cycle"))
        with self.lock, self.db:
            # Serialize read/compare/write across separate watchdog connections,
            # not only threads sharing this Store object.
            self.db.execute("BEGIN IMMEDIATE")
            old = self.db.execute("SELECT status,payload FROM orders WHERE cid=?", (cid,)).fetchone()
            if not old:
                raise ValueError("Order has no durable reservation")
            previous = json.loads(old[1])
            if Decimal(str(order.get("filled_qty", 0) or 0)) < Decimal(str(previous.get("filled_qty", 0) or 0)):
                return
            if old[0] in TERMINAL and status not in TERMINAL:
                return
            self.db.execute("UPDATE orders SET status=?,payload=? WHERE cid=?", (status, json.dumps(order, default=str), cid))
            filled = Decimal(str(order.get("filled_qty", 0) or 0))
            if filled > 0:
                self.db.execute("INSERT OR IGNORE INTO fills VALUES(?,?,?,?)", (
                    cid, str(filled.normalize()), datetime.now(timezone.utc).isoformat(), json.dumps(order, default=str)))
        if previous != order:
            self.event("order", order)
        if status == "rejected":
            self.block("IOC order rejected; verify account permission and broker rejection before resuming")
        elif status in {"reconciliation_required", "replaced", "suspended", "calculated", "stopped"}:
            self.block("Unresolved broker order state: " + status)

    def uncertain(self, cid, reason):
        with self.lock, self.db:
            self.db.execute("UPDATE orders SET status='reconciliation_required' WHERE cid=? AND status NOT IN ('filled','canceled','expired','rejected')", (cid,))
        self.block(reason)

    def pending(self):
        with self.lock:
            return [r[0] for r in self.db.execute("SELECT cid,status FROM orders") if r[1] not in TERMINAL]

    def count(self, day):
        with self.lock:
            return self.db.execute("SELECT count(*) FROM orders WHERE day=?", (day,)).fetchone()[0]

    def get_control(self, key):
        with self.lock:
            row = self.db.execute("SELECT value FROM controls WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_control(self, key, value):
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO controls VALUES(?,?)", (key, value))

    def paused(self):
        return self.get_control("paused") == "true"

    def pause(self, value):
        self.set_control("paused", "true" if value else "false")

    def safety_reason(self):
        return self.get_control("safety_reason")

    def block(self, reason):
        reason = redact(reason)
        if self.safety_reason() != reason:
            self.event("safety_alert", {"reason": reason, "outcome": "new_orders_disabled"})
        self.set_control("safety_reason", reason)
        self.pause(True)

    def acknowledge(self):
        if self.pending():
            raise ValueError("Cannot acknowledge with unresolved orders")
        self.event("operator", {"action": "acknowledge_safety", "reason": self.safety_reason()})
        self.set_control("safety_reason", "")
