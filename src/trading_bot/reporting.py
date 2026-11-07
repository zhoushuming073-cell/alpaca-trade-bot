import json

from .settings import load_config


def report(store):
    accounts = store.recent_accounts(400)
    latest = accounts[-1] if accounts else None
    peak = 0.0
    drawdown = 0.0
    for account in accounts:
        equity = float(account.get("equity") or 0)
        peak = max(peak, equity)
        if peak:
            drawdown = max(drawdown, 1 - equity / peak)
    events = store.recent_events(
        ("one_shot_decision", "one_shot_result", "one_shot_error", "one_shot_research",
         "order", "error", "safety_alert", "operator", "order_not_sent",
         "config_pending", "watchdog_loop"),
        limit=40,
    )
    orders = []
    with store.lock:
        rows = store.db.execute(
            "SELECT cid,day,status,state,stop_status,payload,reason FROM orders "
            "ORDER BY rowid DESC LIMIT 100"
        ).fetchall()
    for row in rows:
        payload = {}
        try:
            payload = json.loads(row["payload"] or "{}")
        except ValueError:
            pass
        orders.append({
            "cid": row["cid"], "day": row["day"], "status": row["status"],
            "state": row["state"], "stop_status": row["stop_status"],
            "reason": row["reason"], **payload,
        })
    try:
        disk = load_config()
        disk_hash = disk.hash
    except Exception:  # noqa: BLE001
        disk_hash = None
    active = store.active_config() or {}
    return {
        "paused": store.paused(),
        "safety_reason": store.safety_reason(),
        "freeze_reason": store.freeze_reason(),
        "config_pending_restart": store.config_pending(),
        "active_config_hash": (active.get("hashes") or {}).get("combined"),
        "disk_config_hash": disk_hash,
        "latest_account": latest,
        "daily_pnl": (
            float(latest["equity"]) - float(latest["last_equity"])
            if latest and latest.get("last_equity") is not None else None
        ),
        "return_since_first_snapshot": (
            float(latest["equity"]) / float(accounts[0]["equity"]) - 1
            if latest and accounts and float(accounts[0].get("equity") or 0) else None
        ),
        "max_observed_drawdown": drawdown,
        "health": {"worker": store.health("worker"), "watchdog": store.health("watchdog")},
        "recent_decisions": store.recent_events(("one_shot_decision",), 12),
        "ai_calls": store.recent_ai_calls(10),
        "orders": orders,
        "unresolved": store.unresolved_orders(),
        "recent_events": events,
    }
