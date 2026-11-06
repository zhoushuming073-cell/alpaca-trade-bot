"""Read-only normalization of real SQLite events for the robot terminal."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .storage import Store

ROBOT_KINDS = (
    "premarket_plan", "universe_scan", "intraday_recheck", "plan",
    "plan_invalidated", "plan_rejected", "premarket_no_trade", "premarket_aborted",
    "premarket_lifecycle", "premarket_data", "quote_errors", "held_data_unavailable",
    "position_review_missing", "intent_rejected", "order_not_sent", "order",
    "order_imported", "order_recovery", "fractional_residual", "startup_reconciliation",
    "after_close_reconcile", "after_close_reconciliation", "database_reconciliation",
    "cutoff_cleanup", "safety_alert", "config_pending", "operator",
    "operator_resolution", "network_retry", "error", "stopped",
)

_AI_KINDS = {
    "premarket_plan", "universe_scan", "intraday_recheck", "plan",
    "plan_invalidated", "plan_rejected", "premarket_no_trade", "premarket_lifecycle",
}
_SYSTEM_KINDS = {
    "error", "network_retry", "safety_alert", "config_pending", "operator",
    "operator_resolution", "quote_errors", "premarket_data", "premarket_aborted",
    "held_data_unavailable", "position_review_missing", "startup_reconciliation",
    "after_close_reconcile", "after_close_reconciliation", "database_reconciliation",
    "cutoff_cleanup", "stopped",
}
_FILL_STATUSES = {"filled", "partially_filled"}
_STATUS_TEXT = {
    "new": "挂单", "pending_new": "提交中", "accepted": "已接受",
    "partially_filled": "部分成交", "filled": "全部成交", "canceled": "已取消",
    "expired": "已到期", "rejected": "已拒绝", "pending_cancel": "撤销中",
    "done_for_day": "当日结束", "replaced": "已替换",
}


def _decisions(payload: dict) -> str:
    if payload.get("decision"):
        return str(payload["decision"])
    if payload.get("market_decision"):
        return "市场判断：" + str(payload["market_decision"])
    if payload.get("status"):
        return str(payload["status"])
    return ""


def _category(kind: str, payload: dict) -> str:
    if kind == "order":
        return "fill" if str(payload.get("status", "")).lower() in _FILL_STATUSES else "execution"
    if kind in {"order_imported", "order_recovery", "fractional_residual"}:
        return "fill"
    if kind in {"intent_rejected", "order_not_sent"}:
        return "execution"
    if kind in _AI_KINDS:
        return "ai"
    return "system"


def _source(kind: str, category: str) -> str:
    if kind in {"premarket_plan", "universe_scan", "intraday_recheck"}:
        return "AI"
    if kind in {"order", "order_imported", "order_recovery"}:
        return "Alpaca"
    if category == "system":
        return "System"
    return "Python"


def _severity(kind: str, payload: dict) -> str:
    if kind in {"safety_alert", "error"}:
        return "critical"
    if kind in {"network_retry", "intent_rejected", "order_not_sent", "quote_errors", "config_pending"}:
        return "warning"
    if kind == "order" and str(payload.get("status", "")).lower() == "rejected":
        return "critical"
    return "info"


def _title(kind: str, payload: dict) -> str:
    if kind == "premarket_plan":
        return "AI 生成盘前计划"
    if kind == "universe_scan":
        return "AI 扫描股票池"
    if kind == "intraday_recheck":
        return "DeepSeek 复核决策"
    if kind == "plan":
        return "交易计划状态更新"
    if kind == "plan_invalidated":
        return "交易计划失效"
    if kind == "plan_rejected":
        return "AI 计划未通过校验"
    if kind == "premarket_no_trade":
        return "AI 今日不交易"
    if kind == "premarket_lifecycle":
        return "盘前分析阶段"
    if kind == "premarket_data":
        return "盘前行情检查"
    if kind == "quote_errors":
        return "行情条件未通过"
    if kind == "intent_rejected":
        return "风控拒绝订单"
    if kind == "order_not_sent":
        return "订单未发送"
    if kind == "order":
        return "Alpaca 订单回报"
    if kind == "order_imported":
        return "Alpaca 历史订单导入"
    if kind == "order_recovery":
        return "订单恢复"
    if kind == "fractional_residual":
        return "碎股处理"
    if kind == "startup_reconciliation":
        return "启动订单对账"
    if kind in {"after_close_reconcile", "after_close_reconciliation"}:
        return "收盘后订单对账"
    if kind == "database_reconciliation":
        return "数据库对账"
    if kind == "cutoff_cleanup":
        return "交易截止清理"
    if kind == "safety_alert":
        return "安全冻结"
    if kind == "config_pending":
        return "配置待重启"
    if kind == "operator":
        return "操作员控制"
    if kind == "operator_resolution":
        return "人工订单处理"
    if kind == "network_retry":
        return "网络重试"
    if kind == "error":
        return "系统异常"
    if kind == "stopped":
        return "机器人停止"
    return "系统事件"


def _summary(kind: str, payload: dict, candidate: dict | None, order: dict | None) -> str:
    if kind == "premarket_plan":
        symbols = payload.get("candidates") or []
        count = len(symbols) if isinstance(symbols, list) else int(symbols or 0)
        symbol_text = "：" + "、".join(symbols[:6]) if isinstance(symbols, list) and symbols else ""
        return f"{payload.get('market_decision', 'AI')} · {count} 个候选" + symbol_text
    if kind == "universe_scan":
        symbols = payload.get("candidates") or []
        if not isinstance(symbols, list):
            symbols = []
        return f"扫描 {len(symbols)} 个候选" + ("：" + "、".join(symbols[:6]) if symbols else "")
    if kind == "intraday_recheck":
        return f"决策 {payload.get('decision', '—')} · {payload.get('reason', '')}".strip()
    if kind == "plan":
        candidate = candidate or {}
        return f"{candidate.get('symbol') or payload.get('plan_id', '')} → {payload.get('status', '')} · {payload.get('reason', '')}".strip()
    if kind == "plan_invalidated":
        return f"{candidate.get('symbol') if candidate else ''} 触发失效条件 · {payload.get('trigger_id', '')}".strip()
    if kind == "plan_rejected":
        return str(payload.get("reason") or payload.get("outcome") or "计划未通过校验")
    if kind == "premarket_no_trade":
        return "AI 判断今天不交易"
    if kind == "premarket_lifecycle":
        return str(payload.get("result") or payload.get("phase") or "盘前阶段更新")
    if kind == "premarket_data":
        return f"可用 {payload.get('available', '—')} 只 · 失败 {len(payload.get('failures') or [])} 只"
    if kind == "quote_errors":
        errors = payload.get("errors") or []
        return "、".join(str(item.get("symbol")) for item in errors[:5]) + (
            f" 等 {len(errors)} 只" if len(errors) > 5 else ""
        )
    if kind == "intent_rejected":
        detail = payload.get("detail")
        return str(payload.get("reason") or detail or "风控拒绝")
    if kind == "order_not_sent":
        return str(payload.get("reason") or payload.get("outcome") or "未发送到券商")
    if kind == "order":
        return f"{payload.get('side', '')} {payload.get('qty', '')} 股 · " \
               f"{_STATUS_TEXT.get(str(payload.get('status', '')).lower(), payload.get('status', ''))} · " \
               f"已成交 {payload.get('filled_qty', '0')}"
    if kind == "order_imported":
        return f"{payload.get('outcome', 'imported')} · {payload.get('cid', '')}"
    if kind == "order_recovery":
        return f"恢复 {payload.get('count', 0)} 笔未决订单"
    if kind == "fractional_residual":
        return f"{payload.get('symbol', '')} 碎股 {payload.get('qty', '')}"
    if kind == "startup_reconciliation":
        return f"检查 {payload.get('checked', 0)} · 更新 {payload.get('updated', 0)} · 未知 {payload.get('unknown', 0)}"
    if kind in {"after_close_reconcile", "after_close_reconciliation"}:
        return f"检查 {payload.get('checked', 0)} · 更新 {payload.get('updated', 0)}"
    if kind == "database_reconciliation":
        return f"订单 {payload.get('orders', 0)} · 成交 {payload.get('fills', 0)} · 活动 {payload.get('activities', 0)} · {payload.get('outcome', '')}"
    if kind == "cutoff_cleanup":
        return str(payload.get("reason") or payload.get("outcome") or "交易截止清理")
    if kind == "safety_alert":
        return str(payload.get("reason") or "新订单已禁用")
    if kind == "config_pending":
        return "配置变更，等待安全重启"
    if kind == "operator":
        return str(payload.get("action") or "控制操作") + (" · " + str(payload.get("source")) if payload.get("source") else "")
    if kind == "operator_resolution":
        return str(payload.get("action") or payload.get("outcome") or "订单处理")
    if kind == "network_retry":
        return f"{payload.get('type', '网络错误')} · {str(payload.get('reason', ''))[:160]}"
    if kind == "error":
        return f"{payload.get('category', '')} · {payload.get('outcome', '')} · {str(payload.get('reason', ''))[:180]}"
    if kind == "stopped":
        return str(payload.get("reason") or "已停止")
    return ""


def _order_lookup(store: Store) -> dict[str, dict]:
    rows = store.recent_orders(300) if hasattr(store, "recent_orders") else []
    result = {}
    for row in rows:
        payload = row.get("payload") or {}
        cid = row.get("cid") or payload.get("client_order_id")
        if cid:
            result[str(cid)] = {"row": row, "payload": payload}
    return result


def build_feed(store: Store, *, after_id: int = 0, limit: int = 200) -> dict:
    rows = store.robot_events(ROBOT_KINDS, after_id=after_id, limit=limit)
    plans = {}
    for plan in store.recent_plans(300):
        candidate = (plan.get("payload") or {}).get("candidate") or {}
        plans[plan["plan_id"]] = {"plan": plan, "candidate": candidate}
    orders = _order_lookup(store)
    events = []
    for row in rows:
        payload = row.get("payload") or {}
        plan_id = payload.get("plan_id")
        plan_entry = plans.get(str(plan_id)) if plan_id else None
        candidate = (plan_entry or {}).get("candidate") or {}
        cid = str(payload.get("cid") or payload.get("client_order_id") or "")
        order_entry = orders.get(cid) if cid else None
        order_payload = (order_entry or {}).get("payload") or {}
        category = _category(row["kind"], payload)
        symbol = (
            payload.get("symbol") or payload.get("ticker")
            or order_payload.get("symbol") or candidate.get("symbol")
        )
        if row["kind"] == "premarket_plan":
            symbols = payload.get("candidates") or []
            if isinstance(symbols, list):
                symbol = "、".join(symbols[:8]) + (f" 等 {len(symbols)} 只" if len(symbols) > 8 else "") if symbols else None
            else:
                symbol = None
        if row["kind"] == "universe_scan":
            symbol = None
        events.append({
            "id": int(row["id"]),
            "ts": row["ts"],
            "kind": row["kind"],
            "category": category,
            "source": _source(row["kind"], category),
            "severity": _severity(row["kind"], payload),
            "symbol": symbol,
            "title": _title(row["kind"], payload),
            "summary": _summary(row["kind"], payload, candidate, order_payload),
            "cid": cid or None,
            "plan_id": plan_id,
            "decision": _decisions(payload),
            "detail": payload,
        })
    monitoring = []
    day = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    now = datetime.now(timezone.utc)
    for plan in store.active_plans(day):
        candidate = (plan.get("payload") or {}).get("candidate") or {}
        try:
            valid = not plan.get("valid_until") or datetime.fromisoformat(
                str(plan["valid_until"]).replace("Z", "+00:00")
            ) > now
        except ValueError:
            valid = False
        if not valid or not candidate.get("symbol"):
            continue
        monitoring.append({
            "symbol": candidate["symbol"],
            "decision": candidate.get("trade_decision"),
            "valid_until": plan.get("valid_until"),
            "triggers": candidate.get("triggers") or [],
        })
    return {
        "events": events,
        "next_after": max((item["id"] for item in events), default=int(after_id or 0)),
        "monitoring": monitoring,
        "server_time": now.isoformat(),
    }
