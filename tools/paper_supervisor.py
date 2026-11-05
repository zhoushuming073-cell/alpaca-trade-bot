"""Local US-trading-day supervisor for the Paper-only AI trading bot.

It is designed to be launched by Windows Task Scheduler once per local day
(20:30 Asia/Shanghai), waits for the next New York session, starts the local
dashboard/worker, and only resumes autopilot if:

* the persisted autopilot flag is true;
* there is no safety freeze / unknown order / config-pending state.

It never creates an order.  The worker itself owns premarket AI, risk checks
and Paper order execution.  The supervisor only starts/stops local processes.
"""

from __future__ import annotations

import argparse
import json
import logging
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas_market_calendars as mcal
import portalocker

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trading_bot.dashboard import PORT, start_worker, worker_environment, worker_processes  # noqa: E402
from trading_bot.settings import load_config  # noqa: E402
from trading_bot.storage import Store  # noqa: E402

ET = ZoneInfo("America/New_York")
LOG = ROOT / "logs" / "supervisor.log"
CAL = mcal.get_calendar("NYSE")


def log(message: str):
    LOG.parent.mkdir(exist_ok=True)
    line = datetime.now(ET).isoformat() + " " + message
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def session_for(day):
    schedule = CAL.schedule(start_date=day.isoformat(), end_date=day.isoformat())
    if schedule.empty:
        return None
    row = schedule.iloc[0]
    return (
        row["market_open"].to_pydatetime().astimezone(ET),
        row["market_close"].to_pydatetime().astimezone(ET),
    )


def port_open() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=0.5):
            return True
    except OSError:
        return False


def ensure_dashboard():
    if port_open():
        return True
    python = ROOT / ".venv/Scripts/python.exe"
    logs = ROOT / "logs"
    logs.mkdir(exist_ok=True)
    out = (logs / "dashboard-supervisor-out.log").open("ab")
    err = (logs / "dashboard-supervisor-error.log").open("ab")
    env = worker_environment()
    subprocess.Popen(
        [str(python), "-m", "trading_bot.dashboard"],
        cwd=ROOT, env=env, stdout=out, stderr=err,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    for _ in range(30):
        if port_open():
            log("dashboard started")
            return True
        time.sleep(1)
    log("dashboard failed to start")
    return False


def autopilot_block_reason(store) -> str | None:
    if store.get_control("autopilot") != "true":
        return "AUTOPILOT_DISABLED"
    if store.freeze_reason():
        return "SAFETY_FROZEN: " + store.freeze_reason()[:300]
    if store.has_unknown_orders():
        return "UNKNOWN_ORDERS"
    if store.config_pending():
        return "CONFIG_PENDING_RESTART"
    try:
        disk = load_config()
    except Exception as exc:  # noqa: BLE001
        return "CONFIG_LOAD_FAILED: " + type(exc).__name__
    active = store.active_config() or {}
    active_hash = (active.get("hashes") or {}).get("combined")
    if active_hash and active_hash != disk.hash:
        return "CONFIG_DRIFT"
    return None


def start_worker_with_retry() -> bool:
    for attempt in range(1, 4):
        if worker_processes():
            log(f"worker already running (attempt {attempt})")
            return True
        start_worker()
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            if worker_processes():
                log(f"worker started (attempt {attempt})")
                return True
            time.sleep(1)
        log(f"worker start attempt {attempt} did not appear")
    return False


def wait_for_worker_health() -> bool:
    store = Store(ROOT / "data/paper.sqlite", role="supervisor-health")
    try:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            health = store.health("worker", 90)
            if health["alive"] and not health["stale"]:
                return True
            time.sleep(2)
        return False
    finally:
        store.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    now = datetime.now(ET)
    session = session_for(now.date())
    if args.check:
        print(json.dumps({
            "now_et": now.isoformat(),
            "session": None if not session else {
                "open": session[0].isoformat(), "close": session[1].isoformat(),
            },
            "worker_running": bool(worker_processes()),
            "dashboard_listening": port_open(),
        }, indent=2))
        return 0
    lock = portalocker.Lock(str(ROOT / "data/supervisor.lock"), timeout=0)
    try:
        lock.acquire()
    except Exception:
        log("another supervisor owns data/supervisor.lock; exits")
        return 0
    try:
        return run_session(now, session)
    finally:
        try:
            lock.release()
        except Exception:
            pass


def run_session(now, session) -> int:
    if not session:
        log("no US session today; supervisor exits")
        return 0
    market_open, market_close = session
    start_at = market_open - timedelta(minutes=35)
    if now >= market_open:
        log("supervisor started after the session open; no premarket plan; exits")
        return 0
    while datetime.now(ET) < start_at:
        time.sleep(min(300, max(5, (start_at - datetime.now(ET)).total_seconds())))
    log("pre-open supervisor wakeup")
    if not ensure_dashboard():
        return 1
    if not start_worker_with_retry():
        log("worker failed to start; autopilot remains paused")
        return 1
    # Engine.heartbeat is first produced by the 09:00 premarket hook.  Do not
    # mistake a normal pre-open initialization wait for a dead worker.
    ready_at = max(datetime.now(ET), market_open + timedelta(minutes=2))
    while datetime.now(ET) < ready_at:
        time.sleep(min(60, max(1, (ready_at - datetime.now(ET)).total_seconds())))
    if not wait_for_worker_health():
        log("worker heartbeat not healthy; autopilot remains paused")
        return 1

    store = Store(ROOT / "data/paper.sqlite", role="supervisor-control")
    try:
        blocked = autopilot_block_reason(store)
        if blocked:
            store.pause(True)
            store.event("operator", {
                "action": "autopilot_blocked", "source": "local_supervisor",
                "reason": blocked,
            })
            log("autopilot blocked: " + blocked)
            return 1
        store.pause(False)
        store.event("operator", {
            "action": "autopilot_resume", "source": "local_supervisor",
        })
        log("autopilot resumed; worker is running")
    finally:
        store.close()

    # The worker owns the session; keep the supervisor alive through the
    # close so this process's lock prevents a duplicate daily start.
    while datetime.now(ET) < market_close + timedelta(minutes=10):
        time.sleep(60)
    log("session finished; supervisor exits")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
