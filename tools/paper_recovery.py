"""Bounded local recovery after network/power interruption.

Runs only when the persisted autopilot flag is true.  It never creates,
cancels, or changes an order.  A WATCHDOG_UNHEALTHY freeze is cleared only
after a fresh authenticated broker read and a clean OrderService reconcile.
Unknown/mismatch/database-restore freezes remain untouched for a human.
"""

from __future__ import annotations

import logging
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import portalocker

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from trading_bot.broker import PaperGateway, build_broker  # noqa: E402
from trading_bot.dashboard import PORT, start_worker, worker_environment, worker_processes  # noqa: E402
from trading_bot.execution import OrderService  # noqa: E402
from trading_bot.settings import load_config, paper_db_path  # noqa: E402
from trading_bot.storage import Store  # noqa: E402

LOG = ROOT / "logs" / "recovery.log"
LOCK = ROOT / "data" / "recovery.lock"
ALLOWED_FREEZES = ("WATCHDOG_UNHEALTHY", "STARTUP_RECONCILIATION_UNAVAILABLE")


def log(message: str):
    LOG.parent.mkdir(exist_ok=True)
    line = datetime.now(timezone.utc).isoformat() + " " + message
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def port_open() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=0.5):
            return True
    except OSError:
        return False


def ensure_dashboard() -> bool:
    if port_open():
        return True
    logs = ROOT / "logs"
    logs.mkdir(exist_ok=True)
    out = (logs / "dashboard-recovery-out.log").open("ab")
    err = (logs / "dashboard-recovery-error.log").open("ab")
    subprocess.Popen(
        [str(ROOT / ".venv/Scripts/python.exe"), "-m", "trading_bot.dashboard"],
        cwd=ROOT, env=worker_environment(), stdout=out, stderr=err,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        | getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    for _ in range(30):
        if port_open():
            return True
        time.sleep(1)
    return False


def wait_worker(started_at: datetime, timeout: int = 90) -> bool:
    store = Store(paper_db_path(), role="recovery-health")
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not worker_processes():
                return False
            health = store.health("worker", 90)
            success = health.get("last_success")
            if success:
                try:
                    success_at = datetime.fromisoformat(success)
                except ValueError:
                    success_at = None
                if success_at and success_at >= started_at and health["alive"] and not health["stale"]:
                    return True
            time.sleep(2)
        return False
    finally:
        store.close()


def start_dashboard_worker() -> bool:
    if not ensure_dashboard():
        log("dashboard failed to start")
        return False
    if worker_processes():
        log("worker already present; checking freshness")
        if wait_worker(datetime.now(timezone.utc), timeout=35):
            return True
        from paper_supervisor import restart_existing_worker

        log("worker heartbeat stale; restarting worker tree")
        if not restart_existing_worker():
            log("old worker tree did not stop")
            return False
    started_at = datetime.now(timezone.utc)
    start_worker()
    if not wait_worker(started_at):
        log("worker did not become healthy")
        return False
    log("worker healthy")
    return True


def main() -> int:
    LOCK.parent.mkdir(exist_ok=True)
    try:
        lock = portalocker.Lock(str(LOCK), timeout=0)
        lock.acquire()
    except Exception:
        log("another recovery process holds the lock")
        return 0
    try:
        return run()
    finally:
        try:
            lock.release()
        except Exception:
            pass


def run() -> int:
    store = Store(paper_db_path(), role="recovery")
    try:
        if store.get_control("autopilot") != "true":
            log("autopilot is off; recovery respects manual stop")
            return 0
        if store.has_unknown_orders():
            log("unknown/conflicting orders; recovery will not touch trading state")
            return 0
        if store.config_pending():
            log("config pending restart; recovery will not resume")
            return 0
        reason = store.safety_reason()
        if reason and not reason.startswith(ALLOWED_FREEZES):
            log("non-network safety freeze retained: " + reason[:300])
            return 0
        if not reason and not store.paused() and worker_processes():
            health = store.health("worker", 90)
            if health["alive"] and not health["stale"]:
                log("worker healthy and autopilot already live; no action")
                return 0

        bundle = load_config()
        try:
            env = worker_environment()
            broker = build_broker({
                "ALPACA_API_KEY": env["ALPACA_API_KEY"],
                "ALPACA_API_SECRET": env["ALPACA_API_SECRET"],
                "DEEPSEEK_API_KEY": env.get("DEEPSEEK_API_KEY", ""),
            }, start_orders_thread=False)
            account = broker.api.get_account()
        except Exception as exc:  # noqa: BLE001
            log("network/broker unavailable: " + type(exc).__name__ + ": " + str(exc)[:300])
            return 0
        if getattr(account, "trading_blocked", False) or getattr(account, "account_blocked", False):
            log("Alpaca account is blocked; recovery stops")
            return 0

        if not start_dashboard_worker():
            return 1

        gateway = PaperGateway(SimpleNamespace(broker=broker), store, bundle.symbols, bundle.execution)
        try:
            reconciled = OrderService(store, gateway, bundle.execution).reconcile()
        except Exception as exc:  # noqa: BLE001
            log("reconciliation failed: " + type(exc).__name__ + ": " + str(exc)[:300])
            return 1
        if reconciled.get("errors") or reconciled.get("unknown") or reconciled.get("conflicts"):
            log("reconciliation not clean: " + str(reconciled)[:500])
            return 1

        if reason:
            store.acknowledge()
            store.event("operator", {
                "action": "auto_recovery_acknowledge",
                "source": "local_recovery",
                "reason": reason,
                "reconciliation": reconciled,
            })
            log("cleared network-induced freeze after clean reconcile")

        store.pause(False)
        store.event("operator", {
            "action": "auto_recovery_resume", "source": "local_recovery",
            "reason": "network/broker reachable and worker healthy",
        })
        log("autopilot resumed after reconnect; no orders were sent")
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
