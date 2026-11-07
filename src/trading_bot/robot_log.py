"""Read-only normalization of real SQLite events for the robot terminal."""

from __future__ import annotations

from datetime import datetime, timezone

from .storage import Store

ROBOT_KINDS = (
    "one_shot_decision", "one_shot_result", "one_shot_error",
    "one_shot_research", "one_shot_research_request", "one_shot_premarket",
    "one_shot_cancel_unknown",
    "premarket_plan", "universe_scan", "intraday_recheck", "plan",
    "plan_invalidated", "plan_rejected", "premarket_no_trade", "premarket_aborted",
    "premarket_lifecycle", "premarket_data", "quote_errors", "held_data_unavailable",
    "position_review_missing", "intent_rejected", "order_not_sent", "order",
    "order_imported", "order_recovery", "fractional_residual", "startup_reconciliation",
    "after_close_reconcile", "after_close_reconciliation", "database_reconciliation",
    "cutoff_cleanup", "safety_alert", "config_pending", "operator",
    "operator_resolution", "network_retry", "error", "stopped", "ai_call",
    "short_check", "short_data",
    "agent_run_started", "agent_run_finished", "agent_run_failed", "agent_report",
    "agent_critique", "agent_symbol_failed", "agent_baseline", "agent_baseline_failed",
    "multi_agent_run",
)

_AI_KINDS = {
    "one_shot_decision", "one_shot_research", "one_shot_research_request", "one_shot_premarket",
    "premarket_plan", "universe_scan", "intraday_recheck", "plan",
    "plan_invalidated", "plan_rejected", "premarket_no_trade", "premarket_lifecycle",
    "ai_call", "agent_run_started", "agent_run_finished", "agent_report",
    "agent_critique", "agent_baseline", "multi_agent_run",
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
    if kind in {"intent_rejected", "order_not_sent", "one_shot_result", "one_shot_cancel_unknown"}:
        return "execution"
    if kind in _AI_KINDS:
        return "ai"
    return "system"


def _source(kind: str, category: str) -> str:
    if kind.startswith("one_shot_"):
        return "AI" if category == "ai" else "Python"
    if kind in {"premarket_plan", "universe_scan", "intraday_recheck", "ai_call"}:
        return "AI"
    if kind in {"short_check", "short_data"}:
        return "System"
    if kind.startswith("agent_") or kind == "multi_agent_run":
        return "AI"
    if kind in {"order", "order_imported", "order_recovery"}:
        return "Alpaca"
    if category == "system":
        return "System"
    return "Python"


def _severity(kind: str, payload: dict) -> str:
    if kind in {"safety_alert", "error"}:
        return "critical"
    if kind in {"network_retry", "intent_rejected", "order_not_sent", "quote_errors", "config_pending",
                "one_shot_error", "one_shot_cancel_unknown"}:
        return "warning"
    if kind == "order" and str(payload.get("status", "")).lower() == "rejected":
        return "critical"
    return "info"


def _title(kind: str, payload: dict) -> str:
    if kind == "one_shot_decision":
        return "OneShot AI 决策"
    if kind == "one_shot_result":
        return "OneShot 订单处理结果"
    if kind == "one_shot_research":
        return "OneShot 盘前研究"
    if kind == "one_shot_research_request":
        return "OneShot 深入研究"
    if kind == "one_shot_premarket":
        return "OneShot 盘前准备完成"
    if kind == "one_shot_error":
        return "OneShot AI 输出错误"
    if kind == "one_shot_cancel_unknown":
        return "撤单结果未知"
    if kind == "premarket_plan":
        return "AI 生成盘前计划"
    if kind == "universe_scan":
        return "AI 扫描股票池"
    if kind == "intraday_recheck":
        return "DeepSeek 复核决策"
    if kind == "ai_call":
        return "DeepSeek 分析请求"
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
    if kind == "short_check":
        return "做空权限 / 借券检查"
    if kind == "short_data":
        return "做空数据检查"
    if kind == "agent_run_started":
        return "三智能体研究开始"
    if kind == "agent_run_finished":
        return "三智能体研究完成"
    if kind == "agent_run_failed":
        return "三智能体研究失败"
    if kind == "agent_report":
        return "智能体报告保存"
    if kind == "agent_critique":
        return "反方分析完成"
    if kind == "agent_baseline":
        return "单智能体影子决策"
    if kind == "agent_baseline_failed":
        return "单智能体影子决策失败"
    if kind == "agent_symbol_failed":
        return "智能体候选失败"
    if kind == "multi_agent_run":
        return "三智能体运行摘要"
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
    if kind == "one_shot_decision":
        intent = payload.get("order_intent") or {}
        return f"{payload.get('action', '—')} {intent.get('symbol', '')} · {payload.get('reason', '')}"[:220]
    if kind == "one_shot_result":
        return f"{payload.get('state') or payload.get('rejected') or '待核对'} · {payload.get('cid') or ''}"
    if kind == "one_shot_research":
        return str(payload.get("summary") or "盘前研究已记录")[:220]
    if kind == "one_shot_research_request":
        return "深入研究 " + "、".join(payload.get("symbols") or [])
    if kind == "one_shot_error":
        return f"{payload.get('stage', '')} · {payload.get('type', '')}"
    if kind == "one_shot_cancel_unknown":
        return str(payload.get("cid") or "撤单状态待核对")
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
    if kind == "ai_call":
        return f"{payload.get('mode', 'AI')} · {payload.get('outcome', '')} · " \
               f"{payload.get('latency_ms', '—')} ms"
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
        intent_label = {
            "sell_to_open": "卖空开仓", "buy_to_close": "买入平空",
            "buy_to_open": "买入开多", "sell_to_close": "卖出平多",
        }.get(str(payload.get("position_intent") or ""), str(payload.get("side", "")))
        return f"{intent_label} {payload.get('qty', '')} 股 · " \
               f"{_STATUS_TEXT.get(str(payload.get('status', '')).lower(), payload.get('status', ''))} · " \
               f"已成交 {payload.get('filled_qty', '0')}"
    if kind == "short_check":
        return f"{payload.get('outcome', '—')} · shortable={payload.get('shortable')} · " \
               f"easy_to_borrow={payload.get('easy_to_borrow')} · " \
               f"buying_power={payload.get('buying_power')}"
    if kind == "short_data":
        return str(payload.get("reason") or "做空数据不可用")[:220]
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
            if isinstance(symbols, list) and symbols:
                symbol = "、".join(symbols[:8]) + (f" 等 {len(symbols)} 只" if len(symbols) > 8 else "")
            else:
                # Older on-disk events stored only the candidate count. Do not
                # invent a symbol list by mixing plans from other runs.
                symbol = None
        if row["kind"] == "universe_scan":
            symbols = payload.get("candidates") or []
            symbol = "、".join(symbols[:8]) + (f" 等 {len(symbols)} 只" if len(symbols) > 8 else "") if isinstance(symbols, list) and symbols else None
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
    now = datetime.now(timezone.utc)
    return {
        "events": events,
        "next_after": max((item["id"] for item in events), default=int(after_id or 0)),
        "server_time": now.isoformat(),
    }
