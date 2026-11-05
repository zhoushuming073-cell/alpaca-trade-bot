"""Loopback-only dashboard and local worker lifecycle controls."""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import psutil
from flask import Flask, jsonify, request, send_from_directory

from .market_data import SessionPhase, session_info
from .settings import ROOT, Execution, config_snapshot, credentials, load_config
from .storage import Store

PORT = 4200
ORIGIN = f"http://127.0.0.1:{PORT}"

EVENT_KINDS = (
    "decision", "plan", "order", "error", "safety_alert", "operator",
    "premarket_plan", "universe_scan", "intraday_recheck", "order_not_sent",
    "order_imported", "order_recovery", "config_pending", "watchdog_loop",
    "premarket_data", "quote_errors", "research_profile", "recheck_suppressed",
    "intent_rejected", "plan_invalidated", "startup_reconciliation",
)


def worker_processes():
    result = []
    for process in psutil.process_iter(["pid", "cmdline", "exe"]):
        try:
            cmd = process.info["cmdline"] or []
            exe = process.info["exe"] or ""
            if (
                "trading_bot.main" in cmd
                and "paper" in cmd
                and Path(exe).resolve() == (ROOT / ".venv/Scripts/python.exe").resolve()
            ):
                result.append(process)
        except (psutil.Error, OSError):
            pass
    return result


def worker_environment():
    credentials()  # local .env wins over legacy files
    env = os.environ.copy()
    legacy = ROOT.parent / "api-key.txt"
    if not env.get("DEEPSEEK_API_KEY") and legacy.exists():
        env["DEEPSEEK_API_KEY"] = legacy.read_text(encoding="utf-8-sig").strip()
    legacy = ROOT.parent / "alpaca key.txt"
    if legacy.exists():
        raw = legacy.read_text(encoding="utf-8-sig")
        for label, key in [("Key", "ALPACA_API_KEY"), ("Secret", "ALPACA_API_SECRET")]:
            match = re.search(r"(?im)^" + label + r"[\s:：=]+(\S+)", raw)
            if not env.get(key) and match:
                env[key] = match[1]
    if not all(env.get(k) for k in ["DEEPSEEK_API_KEY", "ALPACA_API_KEY", "ALPACA_API_SECRET"]):
        raise ValueError("Missing API credentials")
    env["ALPACA_IS_PAPER"] = "true"
    env["PYTHONUTF8"] = "1"
    env["LUMIBOT_HIDE_BOTSPOT_TIP"] = "1"
    return env


def start_worker():
    if worker_processes():
        return
    env = worker_environment()
    (ROOT / "logs").mkdir(exist_ok=True)
    with (ROOT / "logs/worker-console.log").open("ab") as output:
        subprocess.Popen(
            [str(ROOT / ".venv/Scripts/python.exe"), "-m", "trading_bot.main", "paper"],
            cwd=ROOT, env=env, stdout=output, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )


def terminate_workers(wait_seconds: float = 8.0):
    for process in worker_processes():
        try:
            process.terminate()
        except psutil.NoSuchProcess:
            pass
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline and worker_processes():
        time.sleep(0.2)


def _execution_for(active: dict | None):
    if active and isinstance(active.get("execution"), dict):
        try:
            return Execution(**active["execution"])
        except Exception:  # noqa: BLE001
            pass
    try:
        return load_config().execution
    except Exception:  # noqa: BLE001
        return Execution()


def snapshot(store):
    active = store.active_config() or {}
    try:
        disk = load_config()
        disk_hash = disk.hash
        disk_symbols = list(disk.symbols)
        disk_version = disk.policy.version
    except Exception as exc:  # noqa: BLE001
        disk_hash = None
        disk_symbols = []
        disk_version = None
    execution = _execution_for(active)
    now = datetime.now(timezone.utc)
    session = session_info(now, execution)
    accounts = store.recent_accounts(400)
    account = accounts[-1] if accounts else None
    source = "运行快照"
    if account is None and (ROOT / "data/preflight.json").exists():
        try:
            preflight = json.loads((ROOT / "data/preflight.json").read_text(encoding="utf-8"))
            if preflight.get("account"):
                account = {"ts": preflight["timestamp"], **preflight["account"]}
                source = "最近只读接入检查"
        except Exception:  # noqa: BLE001
            pass
    history = [{"ts": item.get("ts"), "equity": item.get("equity")} for item in accounts]
    peak = 0.0
    drawdown = 0.0
    for item in history:
        equity = float(item.get("equity") or 0)
        peak = max(peak, equity)
        if peak:
            drawdown = max(drawdown, 1 - equity / peak)
    age = None
    if account and account.get("ts"):
        try:
            age = (now - datetime.fromisoformat(str(account["ts"]).replace("Z", "+00:00"))).total_seconds()
        except ValueError:
            age = None
    day = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    plans = store.active_plans(day)
    orders_rows = []
    with store.lock:
        rows = store.db.execute(
            "SELECT cid,day,status,state,stop_status,payload,reason FROM orders "
            "ORDER BY rowid DESC LIMIT 100"
        ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["payload"] or "{}")
        except ValueError:
            payload = {}
        orders_rows.append({
            "cid": row["cid"], "day": row["day"], "status": row["status"],
            "state": row["state"], "stop_status": row["stop_status"],
            "reason": row["reason"], **payload,
        })
    health = {"worker": store.health("worker"), "watchdog": store.health("watchdog")}
    frozen = bool(store.freeze_reason())
    phase = "FROZEN" if frozen else session.phase.value
    active_hash = (active.get("hashes") or {}).get("combined")
    pending = bool(store.config_pending() or (disk_hash and active_hash and disk_hash != active_hash))
    symbols = active.get("symbols") if active.get("symbols") else disk_symbols
    version = (active.get("policy") or {}).get("version") or disk_version
    profiles = []
    for symbol in symbols or []:
        row = store.research_profile(symbol)
        if row:
            profiles.append({
                "symbol": symbol, "status": row["status"], "version": row["version"],
                "verified_at": row["verified_at"], "expires_at": row["expires_at"],
            })
    return {
        "service": "ai-paper-trader",
        "now": now.isoformat(),
        "now_et": session.now_et.isoformat(),
        "phase": phase,
        "session_reason": session.reason,
        "window": {
            "PREMARKET": "盘前研究 09:00-09:30 ET",
            "ACTIVE": "主动交易 09:30-10:00 ET",
            "CLOSED": "非主动操作时段",
            "FROZEN": "冻结：需人工核对",
            "NO_SESSION": "休市",
        }.get(phase, phase),
        "running": bool(worker_processes()),
        "paused": store.paused(),
        "safety_reason": store.safety_reason(),
        "freeze_reason": store.freeze_reason(),
        "config_pending_restart": pending,
        "active_config_hash": active_hash,
        "disk_config_hash": disk_hash,
        "account": account,
        "account_source": source,
        "account_age_seconds": age,
        "daily_pnl": (
            float(account["equity"]) - float(account["last_equity"])
            if account and account.get("last_equity") is not None else None
        ),
        "return": (
            float(account["equity"]) / float(history[0]["equity"]) - 1
            if account and history and float(history[0].get("equity") or 0) else None
        ),
        "drawdown": drawdown if history else None,
        "equity_history": history[:: max(1, len(history) // 300)][-300:],
        "execution": execution.model_dump(),
        "broker_stop": (
            "VERIFIED" if execution.allow_broker_stop and execution.broker_stop_verified
            else "UNVERIFIED"
        ),
        "health": health,
        "decision": next(iter(store.recent_events(("premarket_plan", "intraday_recheck", "decision"), 1)), None),
        "events": list(reversed(store.recent_events(EVENT_KINDS, 50))),
        "orders": orders_rows,
        "unresolved": store.unresolved_orders(),
        "plans": [
            {
                "plan_id": plan["plan_id"], "status": plan["status"],
                "valid_until": plan["valid_until"],
                "candidate": (plan.get("payload") or {}).get("candidate"),
            }
            for plan in plans
        ],
        "ai_calls": store.recent_ai_calls(10),
        "research": profiles,
        "research_capabilities": (
            {"status": "UNAVAILABLE", "reason": "not configured"} if not profiles else
            {"status": "AVAILABLE", "profiles": len(profiles)}
        ),
        "symbols": symbols,
        "strategy_version": version,
        "risk": {},
        "control_token": None,  # filled by the route
    }


def create_app(db_path=None):
    app = Flask(__name__)
    db_path = db_path or ROOT / "data/paper.sqlite"
    token = secrets.token_urlsafe(32)
    mutex = threading.Lock()

    @app.before_request
    def local_only():
        if request.host != f"127.0.0.1:{PORT}":
            return jsonify(error="Invalid host"), 403
        if request.method == "POST" and (
            request.headers.get("Origin") != ORIGIN
            or not secrets.compare_digest(request.headers.get("X-Control-Token", ""), token)
        ):
            return jsonify(error="Invalid control request"), 403

    @app.after_request
    def headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; frame-ancestors 'none'"
        )
        return response

    @app.get("/")
    def index():
        return send_from_directory(ROOT / "web", "index.html")

    @app.get("/app.js")
    def js():
        return send_from_directory(ROOT / "web", "app.js")

    @app.get("/api/status")
    def status():
        store = Store(db_path, role="dashboard")
        try:
            data = snapshot(store)
            data["control_token"] = token
            return jsonify(**data)
        finally:
            store.close()

    @app.post("/api/control/<action>")
    def control(action):
        with mutex:
            store = Store(db_path, role="dashboard-control")
            try:
                if action == "pause":
                    store.pause(True)
                elif action == "start":
                    start_worker()
                elif action == "resume":
                    if store.freeze_reason() or store.config_pending():
                        return jsonify(error="安全模式或配置待重启：不能恢复下单。"), 409
                    start_worker()
                    store.pause(False)
                elif action == "stop":
                    store.pause(True)
                    if store.has_unknown_orders():
                        return jsonify(error="存在未知/冲突订单，保留进程以继续对账。"), 409
                    terminate_workers()
                elif action == "restart":
                    store.pause(True)
                    if store.has_unknown_orders():
                        return jsonify(error="存在未知/冲突订单，拒绝安全重启。"), 409
                    terminate_workers()
                    start_worker()
                    # A restart keeps the bot paused; resume is separate.
                else:
                    return jsonify(error="Unknown action"), 400
                store.event("operator", {"action": action, "source": "local_dashboard"})
                return jsonify(ok=True)
            except Exception as exc:  # noqa: BLE001
                return jsonify(error="操作失败：" + type(exc).__name__ + "；请检查本地日志与配置。"), 500
            finally:
                store.close()
    return app


def main():
    import argparse

    import portalocker
    from werkzeug.serving import make_server

    parser = argparse.ArgumentParser()
    parser.add_argument("--start-worker", action="store_true")
    args = parser.parse_args()
    (ROOT / "data").mkdir(exist_ok=True)
    with portalocker.Lock(str(ROOT / "data/dashboard.lock"), timeout=0):
        if args.start_worker:
            start_worker()
        make_server("127.0.0.1", PORT, create_app(), threaded=True).serve_forever()


if __name__ == "__main__":
    main()
