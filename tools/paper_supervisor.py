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
from trading_bot.settings import load_config, paper_db_path  # noqa: E402
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


def wait_for_worker_health(*, until, started_at, clock=None, sleep=None) -> bool:
    clock = clock or (lambda: datetime.now(ET))
    sleep = sleep or time.sleep
    store = Store(paper_db_path(), role="supervisor-health")
    try:
        while clock() < until:
            healthy = True
            for component in ("worker", "watchdog"):
                health = store.health(component, 90)
                success = health.get("last_success")
                success = datetime.fromisoformat(success) if success else None
                if not (health["alive"] and not health["stale"]
                        and health["consecutive_failures"] == 0 and success
                        and success >= started_at and (clock() - success).total_seconds() <= 90):
                    healthy = False
            if healthy:
                return True
            if autopilot_block_reason(store):
                return False
            sleep(min(2, max(0, (until - clock()).total_seconds())))
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


def restart_existing_worker() -> bool:
    """Pre-open only: reload disk code, including the new fallback, each day.

    Windows venv launchers have a child Python process. Stop the entire worker
    tree; terminating only the launcher can leave an old worker trading.
    No broker cancellation or position operation is performed here.
    """
    import psutil
    processes = []
    for parent in worker_processes():
        try:
            processes.extend(parent.children(recursive=True))
            processes.append(parent)
        except psutil.NoSuchProcess:
            continue
    for process in processes:
        try:
            process.terminate()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(processes, timeout=8)
    return not alive


def run_session(now, session, *, clock=None, sleep=None, keep_alive=True) -> int:
    clock = clock or (lambda: datetime.now(ET))
    sleep = sleep or time.sleep
    if not session:
        log("no US session today; supervisor exits")
        return 0
    market_open, market_close = session
    start_at = market_open - timedelta(minutes=35)
    if now >= market_open:
        log("supervisor started after the session open; no premarket plan; exits")
        return 0
    while clock() < start_at:
        sleep(min(60, max(1, (start_at - clock()).total_seconds())))
    if clock() >= market_open:
        log("missed premarket window; no automatic resume")
        return 1
    log("pre-open supervisor wakeup")
    store = Store(paper_db_path(), role="supervisor-control")
    try:
        blocked = autopilot_block_reason(store)
        if blocked:
            log("autopilot blocked: " + blocked)
            return 1
        store.pause(True)
        store.set_alive("worker", False)
        store.set_alive("watchdog", False)
    finally:
        store.close()
    if not ensure_dashboard():
        return 1
    if not restart_existing_worker():
        log("old worker still alive; no duplicate start")
        return 1
    started_at = clock()
    if not start_worker_with_retry():
        log("worker failed to start; autopilot remains paused")
        return 1
    # Initialization/idle heartbeat is independent of the 09:00 AI hook.
    # Resume as soon as BOTH worker and broker reconciler are healthy, ideally
    # before 09:00. A delayed start can catch up, but never resume after 09:30.
    if not wait_for_worker_health(until=market_open, started_at=started_at, clock=clock, sleep=sleep):
        log("worker heartbeat not healthy; autopilot remains paused")
        return 1

    store = Store(paper_db_path(), role="supervisor-control")
    try:
        blocked = autopilot_block_reason(store)
        if clock() >= market_open:
            blocked = "PREMARKET_WINDOW_MISSED"
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
    while keep_alive and clock() < market_close + timedelta(minutes=10):
        sleep(60)
    log("session finished; supervisor exits")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
