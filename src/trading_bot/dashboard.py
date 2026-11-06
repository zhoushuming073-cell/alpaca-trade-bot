"""Loopback-only dashboard and local worker lifecycle controls."""

from __future__ import annotations

import json
import math
import os
import re
import secrets
import subprocess
import threading
import time
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import psutil
from flask import Flask, jsonify, request, send_from_directory

from .broker import PaperGateway, build_broker
from .market_data import SessionPhase, session_info
from .order_state import BROKER_STATUS_TO_STATE
from .settings import ROOT, Execution, config_snapshot, credentials, load_config
from .storage import Store
from .assistant import Companion, AssistantError, AssistantBusy

PORT = 4200
ORIGIN = f"http://127.0.0.1:{PORT}"

EVENT_KINDS = (
    "decision", "plan", "order", "error", "safety_alert", "operator",
    "premarket_plan", "universe_scan", "intraday_recheck", "order_not_sent",
    "order_imported", "order_recovery", "config_pending", "watchdog_loop",
    "premarket_data", "quote_errors", "research_profile", "recheck_suppressed",
    "intent_rejected", "plan_invalidated", "startup_reconciliation",
    "source_call", "source_fetch_error", "research_news",
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


def alpaca_environment():
    """Load only Alpaca Paper credentials for the read-only account feed."""
    credentials()  # local .env wins over legacy files
    env = os.environ.copy()
    legacy = ROOT.parent / "alpaca key.txt"
    if legacy.exists():
        raw = legacy.read_text(encoding="utf-8-sig")
        for label, key in [("Key", "ALPACA_API_KEY"), ("Secret", "ALPACA_API_SECRET")]:
            match = re.search(r"(?im)^" + label + r"[\s:：=]+(\S+)", raw)
            if not env.get(key) and match:
                env[key] = match[1]
    if not all(env.get(k) for k in ["ALPACA_API_KEY", "ALPACA_API_SECRET"]):
        raise ValueError("Missing Alpaca Paper credentials")
    env["ALPACA_IS_PAPER"] = "true"
    return env


def worker_environment():
    env = alpaca_environment()
    legacy = ROOT.parent / "api-key.txt"
    if not env.get("DEEPSEEK_API_KEY") and legacy.exists():
        env["DEEPSEEK_API_KEY"] = legacy.read_text(encoding="utf-8-sig").strip()
    if not env.get("DEEPSEEK_API_KEY"):
        raise ValueError("Missing DeepSeek credentials")
    env["PYTHONUTF8"] = "1"
    env["LUMIBOT_HIDE_BOTSPOT_TIP"] = "1"
    return env


def _finite(value) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def validate_account_snapshot(account: dict) -> dict:
    """Validate a raw Alpaca account/position/order snapshot.

    Invalid or partial snapshots are never converted into zeros or sample
    values; callers keep the last known real snapshot instead.
    """
    if not isinstance(account, dict):
        raise ValueError("account snapshot is not an object")
    required = (
        "equity", "last_equity", "cash", "buying_power", "non_margin_cash",
        "available_non_margin_cash", "positions", "position_details",
        "quantities", "available_quantities", "open_orders",
    )
    for key in required:
        if key not in account:
            raise ValueError(f"account snapshot missing {key}")
    for key in ("equity", "last_equity", "cash", "buying_power",
                "non_margin_cash", "available_non_margin_cash"):
        if not _finite(account.get(key)):
            raise ValueError(f"account snapshot has invalid {key}")
    if not isinstance(account["positions"], dict) or not isinstance(account["position_details"], dict):
        raise ValueError("account positions are invalid")
    if set(account["positions"]) != set(account["position_details"]):
        raise ValueError("account position maps disagree")
    for symbol, detail in account["position_details"].items():
        if not isinstance(detail, dict) or not _finite(detail.get("qty")):
            raise ValueError(f"invalid position detail {symbol}")
        if not _finite(detail.get("market_value")) or not _finite(detail.get("unrealized_pl")):
            raise ValueError(f"invalid position value {symbol}")
    if not isinstance(account["open_orders"], list):
        raise ValueError("open_orders is not a list")
    if "recent_orders" in account and not isinstance(account["recent_orders"], list):
        raise ValueError("recent_orders is not a list")
    if account.get("asof"):
        try:
            datetime.fromisoformat(str(account["asof"]).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("invalid account asof timestamp") from exc
    return account


class LiveAccountFeed:
    """Background read-only Alpaca Paper account/position/order feed.

    The Flask page never blocks on a broker request.  The feed keeps the last
    valid snapshot, retries transient network errors, and marks stale/error
    state explicitly instead of substituting simulated values.
    """

    def __init__(
        self, *, symbols=None, execution=None, interval: float = 5.0,
        stale_after: float = 15.0, factory=None,
    ):
        self.symbols = list(symbols) if symbols is not None else None
        self.execution = execution
        self.interval = max(1.0, float(interval))
        self.stale_after = max(self.interval * 2, float(stale_after))
        self._factory = factory
        self._lock = threading.Lock()
        self._snapshot = None
        self._fetched_at = None
        self._last_attempt = None
        self._error = None
        self._stop = threading.Event()
        self._thread = None

    def _build(self):
        keys = alpaca_environment()
        broker = build_broker(
            {"ALPACA_API_KEY": keys["ALPACA_API_KEY"],
             "ALPACA_API_SECRET": keys["ALPACA_API_SECRET"]},
            start_orders_thread=False,
        )
        return PaperGateway(
            SimpleNamespace(broker=broker), None, self.symbols, self.execution
        )

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="alpaca-paper-account-feed", daemon=True
        )
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        gateway = None
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                if gateway is None:
                    gateway = self._factory() if self._factory else self._build()
                account = gateway.snapshot(include_history=True)
                validate_account_snapshot(account)
                now = datetime.now(timezone.utc)
                with self._lock:
                    self._snapshot = account
                    self._fetched_at = now
                    self._last_attempt = now
                    self._error = None
            except Exception as exc:  # noqa: BLE001 - keep last real snapshot
                gateway = None
                with self._lock:
                    self._error = f"{type(exc).__name__}: {str(exc)[:300]}"
                    self._last_attempt = datetime.now(timezone.utc)
            elapsed = time.monotonic() - started
            self._stop.wait(max(self.interval, self.interval - elapsed))

    def get(self):
        with self._lock:
            account = deepcopy(self._snapshot)
            fetched_at = self._fetched_at
            error = self._error
            last_attempt = self._last_attempt
        now = datetime.now(timezone.utc)
        age = (now - fetched_at).total_seconds() if fetched_at else None
        return {
            "account": account,
            "fetched_at": fetched_at.isoformat() if fetched_at else None,
            "age_seconds": age,
            "stale": bool(age is None or age > self.stale_after),
            "error": error,
            "last_attempt": last_attempt.isoformat() if last_attempt else None,
        }


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


def snapshot(store, account_feed=None):
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
    live = account_feed.get() if account_feed is not None else None
    account = None
    source = "ALPACA PAPER 不可用"
    fetched_at = None
    account_stale = True
    account_error = None
    if live and live.get("account"):
        account = deepcopy(live["account"])
        account["ts"] = account.get("asof") or live.get("fetched_at")
        fetched_at = live.get("fetched_at")
        account_stale = bool(live.get("stale") or live.get("error"))
        account_error = live.get("error")
        source = "ALPACA PAPER 实时" if not account_stale else "ALPACA PAPER 最近成功快照"
    else:
        if live:
            account_error = live.get("error")
        if accounts:
            account = accounts[-1]
            fetched_at = account.get("ts")
            account["ts"] = account.get("ts") or account.get("asof")
            source = "最近一次运行快照"
            account_stale = True
        elif (ROOT / "data/preflight.json").exists():
            try:
                preflight = json.loads((ROOT / "data/preflight.json").read_text(encoding="utf-8"))
                if preflight.get("account"):
                    account = {"ts": preflight["timestamp"], **preflight["account"]}
                    fetched_at = preflight["timestamp"]
                    source = "最近只读接入检查"
                    account_stale = True
            except Exception as exc:  # noqa: BLE001
                account_error = f"preflight_parse: {type(exc).__name__}"
    history = [{"ts": item.get("ts"), "equity": item.get("equity")} for item in accounts]
    peak = 0.0
    drawdown = 0.0
    for item in history:
        equity = float(item.get("equity") or 0)
        peak = max(peak, equity)
        if peak:
            drawdown = max(drawdown, 1 - equity / peak)
    age = None
    if fetched_at:
        try:
            age = (now - datetime.fromisoformat(str(fetched_at).replace("Z", "+00:00"))).total_seconds()
        except ValueError:
            age = None
    if account and not account_stale and account.get("ts"):
        try:
            age = (now - datetime.fromisoformat(str(account["ts"]).replace("Z", "+00:00"))).total_seconds()
        except ValueError:
            pass
    if account and age is not None and age > 120:
        account_stale = True
        if source == "ALPACA PAPER 实时":
            source = "ALPACA PAPER 最近成功快照"
    if account is None:
        source = "ALPACA PAPER 不可用"
        account_error = account_error or "no live Alpaca snapshot and no historical snapshot"
    if account:
        from .risk_manager import stop_orders

        broker_stops = stop_orders(account)
        stop_symbols = {str(order.get("symbol")) for order in broker_stops}
        for symbol, detail in (account.get("position_details") or {}).items():
            if symbol in stop_symbols:
                detail["stop_status"] = "VERIFIED"
                continue
            intent = store.latest_buy_intent(symbol)
            if not intent:
                detail["stop_status"] = "UNKNOWN"
            elif str(intent.get("stop_policy") or "NONE").upper() == "NONE":
                detail["stop_status"] = "NONE_BY_AI"
            else:
                detail["stop_status"] = "FAILED"
    day = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    plans = store.active_plans(day)
    local_rows = []
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
        local_rows.append({
            "cid": row["cid"], "day": row["day"], "status": row["status"],
            "state": row["state"], "stop_status": row["stop_status"],
            "reason": row["reason"], **payload,
        })
    local_by_cid = {row["cid"]: row for row in local_rows if row.get("cid")}
    orders_rows = []
    seen = set()
    for order in (account or {}).get("open_orders", []) or []:
        cid = str(order.get("client_order_id") or "")
        local = local_by_cid.get(cid) if cid else {}
        item = {**order}
        item["cid"] = cid or None
        item["day"] = (local or {}).get("day")
        item["status"] = order.get("status")
        item["local_state"] = (local or {}).get("state")
        broker_state = BROKER_STATUS_TO_STATE.get(
            str(order.get("status") or "").lower(), "BROKER_OPEN"
        )
        broker_state = getattr(broker_state, "value", broker_state)
        item["state"] = broker_state
        item["state_mismatch"] = bool(
            item["local_state"] and item["local_state"] != broker_state
        )
        item["stop_status"] = (local or {}).get("stop_status") or order.get("stop_status")
        item["reason"] = (local or {}).get("reason")
        item["source"] = "ALPACA_OPEN"
        orders_rows.append(item)
        if cid:
            seen.add(cid)
    for order in (account or {}).get("recent_orders", []) or []:
        cid = str(order.get("client_order_id") or "")
        if cid and cid in seen:
            continue
        local = local_by_cid.get(cid) if cid else {}
        local_state = (local or {}).get("state")
        broker_state = BROKER_STATUS_TO_STATE.get(
            str(order.get("status") or "").lower(), "BROKER_CLOSED"
        )
        broker_state = getattr(broker_state, "value", broker_state)
        item = {**order}
        item["cid"] = cid or None
        item["day"] = (local or {}).get("day") or (str(order.get("created_at") or "")[:10] or None)
        item["local_state"] = local_state
        item["state"] = broker_state
        item["state_mismatch"] = bool(local_state and local_state != broker_state)
        item["stop_status"] = (local or {}).get("stop_status") or order.get("stop_status")
        item["reason"] = (local or {}).get("reason")
        item["source"] = "ALPACA_RECENT"
        orders_rows.append(item)
        if cid:
            seen.add(cid)
    # Keep local pre-dispatch/unknown/terminal orders visible even when they
    # are not (or not yet) returned by Alpaca's open-order query.
    for local in local_rows:
        if local.get("cid") and local["cid"] in seen:
            continue
        orders_rows.append({**local, "source": "LOCAL"})
    health = {
        "worker": store.health("worker", execution.watchdog_stale_seconds),
        "watchdog": store.health("watchdog", execution.watchdog_stale_seconds),
    }
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
            profile = row.get("profile") or {}
            evidence = store.research_evidence(symbol, limit=5)
            profiles.append({
                "symbol": symbol, "status": row["status"], "version": row["version"],
                "verified_at": row["verified_at"], "expires_at": row["expires_at"],
                "source_status": profile.get("source_status"),
                "evidence_count": len(evidence),
                "latest_evidence": [
                    {k: item.get(k) for k in (
                        "kind", "source_url", "title", "published_at", "retrieved_at", "verified"
                    )} for item in evidence
                ],
            })
    verified_profiles = sum(
        1 for item in profiles
        if item.get("source_status") == "AVAILABLE" and item.get("status") in {"OK", "STALE"}
    )
    research_capabilities = {
        "status": (
            "AVAILABLE" if verified_profiles else
            "DEGRADED" if profiles else "UNAVAILABLE"
        ),
        "profiles": len(profiles),
        "verified_profiles": verified_profiles,
        "source_note": "profile.source_status is written by the retrieval layer",
    }
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
        "account_stale": account_stale,
        "account_error": account_error,
        "account_fetched_at": fetched_at,
        "account_asof": (account or {}).get("asof") or (account or {}).get("ts"),
        "recent_orders_error": (account or {}).get("recent_orders_error"),
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
        "orders_source": "ALPACA_OPEN+ALPACA_RECENT+LOCAL",
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
        "research_capabilities": research_capabilities,
        "symbols": symbols,
        "strategy_version": version,
        "risk": {},
        "control_token": None,  # filled by the route
    }


def create_app(db_path=None, account_feed=None, companion_model=None):
    app = Flask(__name__)
    db_path = db_path or ROOT / "data/paper.sqlite"
    app.config["MAX_CONTENT_LENGTH"] = 16384
    def read_status():
        store = Store(db_path, role="assistant-read")
        try:
            return snapshot(store, account_feed)
        finally:
            store.close()
    companion = Companion(db_path, read_status, model=companion_model)
    app.extensions["companion"] = companion
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

    @app.get("/api/health")
    def health():
        # Readiness is local: never wait for Alpaca, AI, or account snapshots.
        return jsonify(service="ai-paper-trader", ready=True)

    @app.get("/api/status")
    def status():
        store = Store(db_path, role="dashboard")
        try:
            data = snapshot(store, account_feed)
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
                    store.set_control("autopilot", "false")
                elif action == "start":
                    start_worker()
                elif action == "resume":
                    if store.freeze_reason() or store.config_pending():
                        return jsonify(error="安全模式或配置待重启：不能恢复下单。"), 409
                    start_worker()
                    store.pause(False)
                    store.set_control("autopilot", "true")
                elif action == "stop":
                    store.pause(True)
                    store.set_control("autopilot", "false")
                    if store.has_unknown_orders():
                        return jsonify(error="存在未知/冲突订单，保留进程以继续对账。"), 409
                    terminate_workers()
                    if worker_processes():
                        return jsonify(error="进程未能在安全时限内停止。"), 409
                elif action == "restart":
                    store.pause(True)
                    store.set_control("autopilot", "false")
                    if store.has_unknown_orders():
                        return jsonify(error="存在未知/冲突订单，拒绝安全重启。"), 409
                    terminate_workers()
                    if worker_processes():
                        return jsonify(error="旧进程未停止，拒绝启动新配置。"), 409
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

    @app.get("/api/assistant")
    def assistant_history():
        try:
            after = int(request.args.get("after", "0"))
            before = int(request.args["before"]) if "before" in request.args else None
            if after < 0 or (before is not None and before <= 0):
                raise ValueError()
        except ValueError:
            return jsonify(error="消息页码无效。"), 400
        return jsonify(companion.history(after=after, before=before))

    @app.post("/api/assistant")
    def assistant_ask():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify(error="请输入问题。"), 400
        message, request_id = body.get("message"), body.get("request_id")
        if not isinstance(message, str) or not 1 <= len(message.strip()) <= 500:
            return jsonify(error="问题请控制在 1–500 字。"), 400
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", request_id):
            return jsonify(error="请求标识无效，请重新提问。"), 400
        try:
            return jsonify(companion.ask(message.strip(), request_id, expand=body.get("expand") is True))
        except AssistantBusy as exc:
            return jsonify(error=str(exc)), 429
        except AssistantError as exc:
            return jsonify(error=str(exc)), 502

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
        try:
            bundle = load_config()
            account_feed = LiveAccountFeed(
                symbols=bundle.symbols, execution=bundle.execution, interval=5.0
            )
            account_feed.start()
        except Exception as exc:  # noqa: BLE001 - dashboard still starts read-only
            account_feed = None
            logging_error = f"{type(exc).__name__}: {exc}"
            (ROOT / "logs").mkdir(exist_ok=True)
            with (ROOT / "logs/dashboard-error.log").open("a", encoding="utf-8") as handle:
                handle.write("account feed unavailable: " + logging_error + "\n")
        app = create_app(account_feed=account_feed)
        app.extensions["companion"].start()
        try:
            make_server("127.0.0.1", PORT, app, threaded=True).serve_forever()
        finally:
            app.extensions["companion"].stop_event.set()


if __name__ == "__main__":
    main()
