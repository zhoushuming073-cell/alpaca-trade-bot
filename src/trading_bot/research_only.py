"""RESEARCH_ONLY runner: real reads and real agents, zero broker writes.

The runner deliberately does not call ``Engine.premarket`` (which may persist
plans) or ``OrderService``.  It builds the same validated data snapshot, runs
the in-process researcher/skeptic/portfolio-manager chain, and returns the
shadow plan without writing anything to the executable ``plans`` table.

Two independent write barriers are installed:

1. ``ReadOnlyBroker`` refuses dispatch/cancel and other write methods.
2. The underlying Alpaca Paper client is marked ``read_only`` and refuses
   POST/PATCH/PUT/DELETE before any request leaves the process.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace

from .broker import PaperGateway, build_broker
from .deepseek_client import DeepSeek
from .engine import Engine
from .market_data import session_info
from .settings import paper_db_path, load_config
from .storage import Store


class ResearchOnlyError(RuntimeError):
    pass


class ReadOnlyBroker:
    """Proxy that exposes reads and hard-fails every broker write method."""

    _WRITE_METHODS = {
        "dispatch", "cancel", "cancel_all", "submit_order", "submit_orders",
        "replace_order", "close_position", "close_all_positions", "liquidate",
    }

    def __init__(self, inner):
        object.__setattr__(self, "_inner", inner)

    def __getattr__(self, name):
        if name in self._WRITE_METHODS:
            raise ResearchOnlyError(f"RESEARCH_ONLY: broker write method forbidden: {name}")
        return getattr(self._inner, name)

    def dispatch(self, *args, **kwargs):
        raise ResearchOnlyError("RESEARCH_ONLY: dispatch forbidden")

    def cancel(self, *args, **kwargs):
        raise ResearchOnlyError("RESEARCH_ONLY: cancel forbidden")


def _json(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


def _plan_count(store: Store) -> int:
    with store.lock:
        return int(store.db.execute("SELECT COUNT(*) FROM plans").fetchone()[0])


def _order_count(store: Store) -> int:
    with store.lock:
        return int(store.db.execute("SELECT COUNT(*) FROM orders").fetchone()[0])


def run_research_only(
    *,
    bundle=None,
    store=None,
    broker=None,
    ai=None,
    data=None,
    daily_data=None,
    clock=None,
    close_store=False,
    deadline_utc=None,
):
    """Run one real/synthetic shadow research cycle.

    Testing may inject a fake broker/AI/data.  With no injection, credentials
    come from the existing local environment/legacy files and all Alpaca calls
    are read-only.
    """
    clock = clock or (lambda: datetime.now(timezone.utc))
    bundle = bundle or load_config()
    if not bundle.multi_agent.enabled:
        bundle = replace(
            bundle,
            multi_agent=bundle.multi_agent.model_copy(update={"enabled": True}),
            hashes={**bundle.hashes, "combined": "research-only-" + bundle.hash},
        )
    owned = store is None
    store = store or Store(paper_db_path(), role="research-only")
    try:
        if broker is None:
            # Imported lazily so an offline injection never reads credentials.
            from .dashboard import worker_environment

            env = worker_environment()
            keys = {
                "ALPACA_API_KEY": env["ALPACA_API_KEY"],
                "ALPACA_API_SECRET": env["ALPACA_API_SECRET"],
                "DEEPSEEK_API_KEY": env["DEEPSEEK_API_KEY"],
            }
            raw = build_broker(keys, start_orders_thread=False, read_only=True)
            gateway = PaperGateway(
                SimpleNamespace(broker=raw), store, bundle.symbols, bundle.execution,
                read_only=True,
            )
            broker = ReadOnlyBroker(gateway)
            if daily_data is None:
                from lumibot.entities import Asset

                daily_data = lambda symbol: raw.data_source.get_historical_prices(
                    Asset(symbol), 160, timestep="day"
                ).df
            if data is None:
                from lumibot.entities import Asset

                data = lambda symbol: raw.data_source.get_historical_prices(
                    Asset(symbol), 80, timestep="5 minutes", include_after_hours=False
                ).df
            if ai is None:
                ai = DeepSeek(
                    keys["DEEPSEEK_API_KEY"], audit=store.event, record=store.ai_call,
                    input_record=store.input_event,
                    max_tokens=bundle.execution.ai_max_tokens,
                    temperature=bundle.execution.ai_temperature,
                    repair_attempts=bundle.execution.ai_repair_attempts,
                )
        broker = broker if isinstance(broker, ReadOnlyBroker) else ReadOnlyBroker(broker)
        if ai is None:
            raise ResearchOnlyError("RESEARCH_ONLY requires an AI client")
        engine = Engine(
            store, broker, ai, bundle,
            data=data, daily_data=daily_data, clock=clock,
            mode="research-only", require_watchdog=False, check_disk_config=False,
        )
        now = clock()
        session = session_info(now, bundle.execution)
        account = engine._account(now)
        account_ctx, held = engine._account_context(account)
        history, failures, unavailable = engine._collect_universe(session)
        if not history:
            raise ResearchOnlyError("No valid history; refusing to fabricate a research snapshot")
        before_plans = _plan_count(store)
        before_orders = _order_count(store)
        run_deadline = deadline_utc
        if run_deadline is None and not session.premarket:
            run_deadline = now + timedelta(seconds=300)
        result = engine._multi_agent_premarket(
            session, account_ctx, history, held, unavailable, run_mode="RESEARCH_ONLY",
            deadline_utc=run_deadline,
        )
        after_plans = _plan_count(store)
        after_orders = _order_count(store)
        shadow_plan = _json(result.plan) if result.plan is not None else None
        baseline = _json((result.baseline or {}).get("plan")) if result.baseline else None
        summary = {
            "mode": "RESEARCH_ONLY",
            "as_of_utc": now.astimezone(timezone.utc).isoformat(),
            "as_of_et": session.now_et.isoformat(),
            "in_premarket_window": bool(session.premarket),
            "window_note": (
                "inside 09:00-09:30 ET research window"
                if session.premarket else
                "outside 09:00-09:30 ET; this is a non-executable shadow snapshot"
            ),
            "market_open": session.market_open.isoformat() if session.market_open else None,
            "run_id": result.run_id,
            "status": result.status,
            "error": result.error,
            "history_symbols": len(history),
            "unavailable": unavailable,
            "failures": failures,
            "held": held,
            "focus": [row.get("symbol") for row in result.focus],
            "reports": [
                {
                    "symbol": row["symbol"], "report_id": row["report_id"],
                    "conclusion": row["report"].get("research_conclusion"),
                    "technical_structure": row["report"].get("technical_structure"),
                    "supporting_evidence_ids": row["report"].get("supporting_evidence_ids"),
                    "counterevidence_ids": row["report"].get("counterevidence_ids"),
                } for row in result.reports
            ],
            "critiques": [
                {
                    "symbol": row["symbol"], "critique_id": row["critique_id"],
                    "assessment": row["critique"].get("overall_assessment"),
                    "severity": row["critique"].get("critique_severity"),
                    "major_risks": row["critique"].get("major_risks"),
                } for row in result.critiques
            ],
            "shadow_plan": shadow_plan,
            "baseline": baseline,
            "baseline_status": (result.baseline or {}).get("report_id"),
            "usage": {
                "calls": result.usage.calls,
                "prompt_tokens": result.usage.prompt_tokens,
                "completion_tokens": result.usage.completion_tokens,
                "total_tokens": result.usage.tokens,
                "latency_ms": result.usage.latency_ms,
                "cost_usd": result.usage.cost_usd if result.usage.cost_known else None,
                "cost_known": result.usage.cost_known,
            },
            "orders_written": after_orders - before_orders,
            "plans_written": after_plans - before_plans,
            "broker_writes_allowed": False,
        }
        store.event("research_only_summary", {
            "run_id": result.run_id, "status": result.status,
            "in_premarket_window": summary["in_premarket_window"],
            "orders_written": summary["orders_written"],
            "plans_written": summary["plans_written"],
        })
        return summary
    finally:
        if owned or close_store:
            store.close()
