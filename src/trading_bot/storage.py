import json
import sqlite3
from datetime import datetime, timezone

TERMINAL = {"filled", "canceled", "expired", "rejected"}

class Store:
    def __init__(self, path):
        self.path=path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, ts TEXT, kind TEXT, payload TEXT);
        CREATE TABLE IF NOT EXISTS cycles(id TEXT PRIMARY KEY, status TEXT);
        CREATE TABLE IF NOT EXISTS orders(cid TEXT PRIMARY KEY, day TEXT, status TEXT, payload TEXT);
        CREATE TABLE IF NOT EXISTS controls(key TEXT PRIMARY KEY, value TEXT);
        ''')
        self.db.commit()

    def event(self, kind, payload):
        self.db.execute("INSERT INTO events(ts,kind,payload) VALUES(?,?,?)", (datetime.now(timezone.utc).isoformat(),kind,json.dumps(payload,default=str,allow_nan=False)))
        self.db.commit()

    def claim(self, cycle):
        cursor = self.db.execute("INSERT OR IGNORE INTO cycles VALUES(?, 'started')", (cycle,))
        self.db.commit()
        return bool(cursor.rowcount)

    def finish(self, cycle, status):
        self.db.execute("UPDATE cycles SET status=? WHERE id=?",(status,cycle)); self.db.commit()

    def reserve(self, cid, day, payload):
        self.db.execute("INSERT INTO orders VALUES(?,?,?,?)",(cid,day,"unknown",json.dumps(payload)))
        self.db.commit()

    def order(self, cid, order):
        self.db.execute("UPDATE orders SET status=?,payload=? WHERE cid=?",(order["status"],json.dumps(order,default=str),cid)); self.db.commit()
        self.event("order",order)

    def pending(self):
        return [r[0] for r in self.db.execute("SELECT cid,status FROM orders") if r[1] not in TERMINAL]

    def count(self, day):
        return self.db.execute("SELECT count(*) FROM orders WHERE day=?",(day,)).fetchone()[0]

    def paused(self):
        row=self.db.execute("SELECT value FROM controls WHERE key='paused'").fetchone()
        return bool(row and row[0]=='true')

    def pause(self, value):
        self.db.execute("INSERT OR REPLACE INTO controls VALUES('paused',?)",('true' if value else 'false',)); self.db.commit()
