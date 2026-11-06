"""In-process three-agent research and decision orchestration.

There are no long-running agent services here.  A premarket run creates three
short-lived, role-isolated DeepSeek calls over one shared API client:

1. MarketResearchAgent scans the fixed watchlist and deep-researches selected
   symbols;
2. SkepticalAnalystAgent critiques each deep report;
3. PortfolioManagerAgent receives only those structured results plus current
   account/order facts and returns the existing strict ``PremarketPlan``.

The orchestrator never receives a broker/client capable of submitting an order.
Only the existing Engine may persist the returned plan, and only the existing
OrderService/risk gateway may submit it after a numeric trigger fires.
"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable

from .agent_schemas import (
    CritiqueReport,
    ResearchReport,
    ResearchScan,
    parse_critique,
    parse_research_report,
    parse_research_scan,
)
from .decision_schema import PremarketPlan, parse_plan, parse_recheck
from .deepseek_client import AICallError
from .safety import redact


class MultiAgentError(RuntimeError):
    """A research/decision run failed closed; no order may be derived from it."""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


class BudgetExceeded(MultiAgentError):
    pass


class AgentCallError(MultiAgentError):
    pass


@dataclass
class Usage:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    cost_usd: float = 0.0
    cost_known: bool = False

    @property
    def tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class OrchestrationResult:
    run_id: str
    plan: PremarketPlan | None
    status: str
    usage: Usage
    reports: list[dict] = field(default_factory=list)
    critiques: list[dict] = field(default_factory=list)
    blocked_symbols: list[str] = field(default_factory=list)
    baseline: dict | None = None
    error: str = ""
    focus: list[dict] = field(default_factory=list)


@dataclass
class RecheckResult:
    run_id: str
    response: object | None
    status: str
    usage: Usage
    reports: list[dict] = field(default_factory=list)
    critiques: list[dict] = field(default_factory=list)
    error: str = ""


class _Budget:
    def __init__(self, config, clock):
        self.config = config
        self.clock = clock
        self.lock = threading.Lock()
        self.usage = Usage()
        self.max_tokens = config.budget.effective_max_tokens
        self.max_cost = config.budget.effective_max_cost

    def check(self, role: str, estimated_tokens: int) -> None:
        with self.lock:
            if self.max_tokens is not None and self.usage.tokens + max(0, int(estimated_tokens)) > self.max_tokens:
                raise BudgetExceeded(
                    "TOKEN_BUDGET",
                    f"{role} estimated input+output would exceed {self.max_tokens} session tokens; "
                    f"used={self.usage.tokens} estimated={estimated_tokens}",
                )
            if self.usage.cost_known and self.usage.cost_usd >= self.max_cost:
                raise BudgetExceeded("COST_BUDGET", f"session cost {self.usage.cost_usd:.4f} reached")

    def record(self, meta: dict) -> float | None:
        prompt = int(meta.get("prompt_tokens") or 0)
        completion = int(meta.get("completion_tokens") or 0)
        cost = None
        rates = self.config.budget
        if rates.cost_per_1m_input_usd or rates.cost_per_1m_output_usd:
            cost = (
                prompt / 1_000_000 * rates.cost_per_1m_input_usd
                + completion / 1_000_000 * rates.cost_per_1m_output_usd
            )
        with self.lock:
            self.usage.calls += 1
            self.usage.prompt_tokens += prompt
            self.usage.completion_tokens += completion
            self.usage.latency_ms += int(meta.get("latency_ms") or 0)
            if cost is not None:
                self.usage.cost_usd += cost
                self.usage.cost_known = True
            if self.max_tokens is not None and self.usage.tokens > self.max_tokens:
                raise BudgetExceeded(
                    "TOKEN_BUDGET", f"session tokens {self.usage.tokens} exceeded {self.max_tokens}"
                )
            if self.usage.cost_known and self.usage.cost_usd > self.max_cost:
                raise BudgetExceeded(
                    "COST_BUDGET", f"session cost {self.usage.cost_usd:.4f} exceeded {self.max_cost:.4f}"
                )
            return cost


class MultiAgentOrchestrator:
    """Runs the three roles and persists every raw/parsed result separately."""

    def __init__(
        self,
        store,
        ai,
        config,
        *,
        clock=None,
        run_id_factory: Callable[[], str] | None = None,
    ):
        self.store = store
        self.ai = ai
        self.config = config.multi_agent
        self.symbols = tuple(config.symbols)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.budget = _Budget(self.config, self.clock)
        self.run_id_factory = run_id_factory or (
            lambda: "ma-" + self.clock().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
        )

    # ---------------------------------------------------------------- public
    def run_premarket(
        self,
        *,
        session,
        base_payload: dict,
        universe: list[dict],
        held: dict,
        focus_loader: Callable[[list[str]], list[dict]],
        deadline_utc: datetime,
        universe_version: str,
        trading_day: str,
        mode: str = "THREE_AGENT",
    ) -> OrchestrationResult:
        if not self.config.enabled:
            raise MultiAgentError("DISABLED", "multi-agent mode is not enabled")
        run_id = self.run_id_factory()
        now = self.clock()
        snapshot_hash = _hash({
            "base": base_payload, "universe": universe, "held": held,
            "universe_version": universe_version,
        })
        self.store.create_agent_run({
            "run_id": run_id, "trading_day": trading_day, "mode": mode,
            "status": "RUNNING", "as_of_utc": base_payload.get("as_of_utc"),
            "universe_version": universe_version, "config_hash": getattr(self.config, "hash", None)
            or _hash({"multi_agent": self.config.model_dump()}),
            "snapshot_hash": snapshot_hash,
        })
        self.store.event("agent_run_started", {
            "run_id": run_id, "trading_day": trading_day,
            "universe_version": universe_version,
        })
        reports: list[dict] = []
        critiques: list[dict] = []
        blocked: list[str] = []
        try:
            self._deadline(deadline_utc, "before scan")
            memory = self.store.agent_memory(self.symbols, per_symbol=2)
            feedback = self.store.agent_feedback(limit=60)
            scan_payload = dict(base_payload)
            scan_payload.update({
                "run_id": run_id,
                "universe_version": universe_version,
                "universe": universe,
                "held_symbols": sorted(held),
                "research_memory": memory,
                "prior_plan_feedback": feedback,
                "agent_role": "researcher",
                "deadline_utc": deadline_utc,
            })
            scan = self._call(
                role="researcher", mode="RESEARCHER_SCAN", payload=scan_payload,
                parser=lambda raw: parse_research_scan(raw, run_id=run_id, now=self.clock()),
                symbol="", report_kind="scan",
            )
            scan = _with_ids(scan, run_id)
            self._save_report(
                run_id=run_id, role="researcher_scan", symbol="",
                report_id=f"scan-{run_id}", parsed=scan,
                meta=scan_payload.get("_meta", {}), status="OK",
            )
            selected = self._select(scan, held)
            if not selected and not held:
                self._finish(run_id, "NO_TRADE", "no research candidates and no holdings")
                plan = _empty_plan(base_payload, run_id, "NO_TRADE")
                return OrchestrationResult(
                    run_id, plan, "NO_TRADE", self.budget.usage,
                    reports, critiques, blocked, focus=[],
                )
            focus_symbols = list(dict.fromkeys(selected + list(held)))
            self._deadline(deadline_utc, "before focus data")
            focus_rows = focus_loader(focus_symbols)
            row_by_symbol = {
                row.get("symbol"): row for row in focus_rows if row.get("symbol")
            }
            selected = [symbol for symbol in selected if symbol in row_by_symbol]
            tasks = list(selected)
            if tasks:
                workers = max(1, min(
                    int(self.config.execution.max_parallel_requests), len(tasks), 4,
                ))
                with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="agents") as pool:
                    futures = {
                        pool.submit(
                            self._research_and_critique,
                            run_id, symbol, row_by_symbol[symbol],
                            base_payload, memory, deadline_utc,
                        ): symbol for symbol in tasks
                    }
                    for future in futures:
                        symbol = futures[future]
                        try:
                            report, critique = future.result()
                        except Exception as exc:  # noqa: BLE001 - fail this symbol closed
                            blocked.append(symbol)
                            self.store.event("agent_symbol_failed", {
                                "run_id": run_id, "symbol": symbol,
                                "reason": type(exc).__name__ + ": " + str(exc)[:400],
                            })
                            continue
                        reports.append(report)
                        critiques.append(critique)
            self._deadline(deadline_utc, "before portfolio manager")
            pm_payload = self._pm_payload(
                base_payload, run_id, universe_version, focus_rows, reports,
                critiques, held, blocked, deadline_utc,
            )
            plan = self._call(
                role="portfolio_manager", mode="PORTFOLIO_MANAGER", payload=pm_payload,
                parser=lambda raw: parse_plan(raw, symbols=self.symbols, now=self.clock()),
                symbol="", report_kind="decision",
            )
            self._validate_plan(plan, run_id, base_payload, reports, critiques, blocked)
            pm_id = f"pm-{run_id}"
            self._save_report(
                run_id=run_id, role="portfolio_manager", symbol="",
                report_id=pm_id, parsed=plan, meta=pm_payload.get("_meta", {}), status="OK",
            )
            plan = plan.model_copy(update={
                "run_id": run_id,
                "decision_mode": "THREE_AGENT",
                "research_report_ids": [report["report_id"] for report in reports],
                "critique_report_ids": [critique["critique_id"] for critique in critiques],
            })
            status = "COMPLETE" if plan.market_decision.value == "TRADE" else "NO_TRADE"
            self._finish(run_id, status, "")
            three_tokens = self.budget.usage.tokens
            three_latency = self.budget.usage.latency_ms
            baseline = self._baseline(
                run_id, base_payload, focus_rows, held, blocked, deadline_utc,
            )
            self._record_experiment(
                run_id, trading_day, snapshot_hash, plan, baseline,
                three_tokens=three_tokens, three_latency=three_latency,
            )
            self.store.event("agent_run_finished", {
                "run_id": run_id, "status": status,
                "reports": len(reports), "critiques": len(critiques),
                "blocked": blocked, "tokens": self.budget.usage.tokens,
                "cost_usd": self.budget.usage.cost_usd if self.budget.usage.cost_known else None,
            })
            return OrchestrationResult(
                run_id, plan, status, self.budget.usage, reports, critiques, blocked,
                baseline, focus=focus_rows,
            )
        except Exception as exc:  # noqa: BLE001 - guaranteed fail-closed
            self.store.update_agent_run(run_id, status="FAILED", error=str(exc)[:1000])
            self.store.event("agent_run_failed", {
                "run_id": run_id, "reason": type(exc).__name__ + ": " + str(exc)[:500],
                "tokens": self.budget.usage.tokens,
            })
            return OrchestrationResult(
                run_id, None, "FAILED", self.budget.usage, reports, critiques, blocked,
                error=str(exc)[:1000], focus=focus_rows if "focus_rows" in locals() else [],
            )

    def run_recheck(
        self,
        *,
        session,
        base_payload: dict,
        symbol: str,
        existing_plan: dict,
        focus_row: dict,
        deadline_utc: datetime,
        trading_day: str,
    ) -> RecheckResult:
        """Bounded researcher -> skeptic -> PM update for one existing plan."""
        if not self.config.enabled:
            raise MultiAgentError("DISABLED", "multi-agent mode is not enabled")
        run_id = self.run_id_factory()
        self.store.create_agent_run({
            "run_id": run_id, "trading_day": trading_day, "mode": "THREE_AGENT_RECHECK",
            "status": "RUNNING", "as_of_utc": base_payload.get("as_of_utc"),
            "universe_version": base_payload.get("strategy", {}).get("version"),
            "config_hash": _hash({"multi_agent": self.config.model_dump()}),
            "snapshot_hash": _hash({"base": base_payload, "plan": existing_plan, "focus": focus_row}),
        })
        reports: list[dict] = []
        critiques: list[dict] = []
        try:
            self._deadline(deadline_utc, "recheck research")
            memory = self.store.agent_memory([symbol], per_symbol=3)
            report, critique = self._research_and_critique(
                run_id, symbol, focus_row, base_payload, memory, deadline_utc,
            )
            reports.append(report)
            critiques.append(critique)
            self._deadline(deadline_utc, "recheck portfolio manager")
            payload = {
                **{k: v for k, v in base_payload.items()
                   if k not in {"research", "news", "universe", "focus"}},
                "run_id": run_id,
                "symbol": symbol,
                "plan_id": existing_plan.get("plan_id"),
                "existing_plan": existing_plan,
                "focus": focus_row,
                "research_reports": [{**report["report"], "report_id": report["report_id"]}],
                "critique_reports": [{**critique["critique"], "critique_id": critique["critique_id"]}],
                "research_memory": memory.get(symbol, []),
                "prior_plan_feedback": self.store.agent_feedback(symbol=symbol, limit=20),
                "deadline_utc": deadline_utc,
            }
            response = self._call(
                role="portfolio_manager", mode="PORTFOLIO_RECHECK", payload=payload,
                parser=lambda raw: parse_recheck(raw, plan_id=str(existing_plan.get("plan_id")), now=self.clock()),
                symbol=symbol, report_kind="recheck",
            )
            if getattr(response, "run_id", "") != run_id:
                raise MultiAgentError("RECHECK_RUN_ID", "portfolio manager run_id mismatch")
            if response.decision.value == "REPLACE":
                if not response.order_intent or response.order_intent.symbol != symbol:
                    raise MultiAgentError("RECHECK_ORDER", "replacement order is not for the rechecked symbol")
                if report["report_id"] not in (response.research_report_ids or []):
                    raise MultiAgentError("RECHECK_EVIDENCE", "replacement lacks research report id")
                if critique["critique_id"] not in (response.critique_report_ids or []):
                    raise MultiAgentError("RECHECK_EVIDENCE", "replacement lacks critique report id")
            self._save_report(
                run_id=run_id, role="portfolio_manager", symbol=symbol,
                report_id=f"pm-recheck-{run_id}", parsed=response,
                meta=payload.get("_meta", {}), status="OK",
            )
            status = response.decision.value
            self.store.update_agent_run(run_id, status=status)
            self.store.event("agent_recheck_finished", {
                "run_id": run_id, "symbol": symbol, "decision": status,
                "reports": len(reports), "critiques": len(critiques),
            })
            return RecheckResult(run_id, response, status, self.budget.usage, reports, critiques)
        except Exception as exc:  # noqa: BLE001
            self.store.update_agent_run(run_id, status="FAILED", error=str(exc)[:1000])
            self.store.event("agent_recheck_failed", {
                "run_id": run_id, "symbol": symbol,
                "reason": type(exc).__name__ + ": " + str(exc)[:500],
            })
            return RecheckResult(
                run_id, None, "FAILED", self.budget.usage, reports, critiques,
                error=str(exc)[:1000],
            )

    # ------------------------------------------------------------- role calls
    def _role(self, role: str):
        mapping = {
            "researcher": self.config.agents.researcher,
            "skeptic": self.config.agents.skeptic,
            "portfolio_manager": self.config.agents.portfolio_manager,
            "baseline": self.config.agents.baseline,
        }
        return mapping[role]

    def _call(self, *, role: str, mode: str, payload: dict, parser, symbol: str,
              report_kind: str):
        role_config = self._role(role)
        if not role_config.enabled:
            raise AgentCallError("ROLE_DISABLED", role)
        clean_payload = {key: value for key, value in payload.items() if not key.startswith("_")}
        deadline = clean_payload.get("deadline_utc")
        if isinstance(deadline, datetime):
            self._deadline(deadline, role)
        cache_key = _hash({
            "run_id": payload.get("run_id"), "role": role, "mode": mode,
            "model": role_config.model, "payload": clean_payload,
        })
        cached = self.store.agent_cache_get(cache_key)
        if cached:
            try:
                parsed = parser(json.dumps(cached["output"], ensure_ascii=False))
                self.store.ai_call({
                    "mode": mode, "model": role_config.model, "run_id": payload.get("run_id"),
                    "agent_role": role, "outcome": "ok", "cached": 1,
                    "latency_ms": 0, "prompt_tokens": 0, "completion_tokens": 0,
                    "cost_usd": 0.0,
                })
                meta = {"cached": True, "model": role_config.model}
                payload["_meta"] = meta
                return _with_meta(parsed, meta)
            except Exception:  # noqa: BLE001 - bad cache must not authorize anything
                pass
        self.budget.check(role, _estimate_tokens(clean_payload) + role_config.max_tokens)
        last_error = None
        for attempt in range(role_config.max_retries + 1):
            self._deadline(deadline, role)
            try:
                parsed, meta = self.ai.complete(
                    mode, clean_payload, parser,
                    model=role_config.model,
                    max_tokens=role_config.max_tokens,
                    temperature=role_config.temperature,
                    timeout=role_config.timeout_seconds,
                    role=role,
                    run_id=payload.get("run_id"),
                    include_raw=True,
                )
                cost = self.budget.record(meta)
                meta["cost_usd"] = cost
                if bool(getattr(self.ai, "records_calls", False)):
                    self.store.set_agent_call_cost(payload.get("run_id"), role, cost)
                else:
                    self.store.ai_call({
                        "mode": mode, "model": meta.get("model") or role_config.model,
                        "prompt_hash": meta.get("prompt_hash"),
                        "input_hash": meta.get("input_hash"),
                        "output_hash": meta.get("output_hash"),
                        "prompt_tokens": meta.get("prompt_tokens"),
                        "completion_tokens": meta.get("completion_tokens"),
                        "latency_ms": meta.get("latency_ms"),
                        "finish_reason": meta.get("finish_reason"),
                        "outcome": "ok", "run_id": payload.get("run_id"),
                        "agent_role": role, "cost_usd": cost,
                    })
                output = _jsonable(parsed)
                self.store.agent_cache_put(
                    cache_key=cache_key, role=role, model=role_config.model,
                    input_hash=meta.get("input_hash") or "", output=output,
                )
                payload["_meta"] = meta
                parsed_with_meta = _with_meta(parsed, meta)
                return parsed_with_meta
            except (AICallError, BudgetExceeded, ValueError, TypeError) as exc:
                last_error = exc
                if not bool(getattr(self.ai, "records_calls", False)):
                    self.store.ai_call({
                        "mode": mode, "model": role_config.model,
                        "run_id": payload.get("run_id"), "agent_role": role,
                        "outcome": "error", "error": str(exc)[:1000],
                    })
                if isinstance(exc, BudgetExceeded) or attempt >= role_config.max_retries:
                    break
            except Exception as exc:  # noqa: BLE001 - provider/transport/parser failure
                last_error = exc
                if not bool(getattr(self.ai, "records_calls", False)):
                    self.store.ai_call({
                        "mode": mode, "model": role_config.model,
                        "run_id": payload.get("run_id"), "agent_role": role,
                        "outcome": "error", "error": str(exc)[:1000],
                    })
                if attempt >= role_config.max_retries:
                    break
        raise AgentCallError("ROLE_FAILED", f"{role}: {type(last_error).__name__}: {last_error}")

    # -------------------------------------------------------------- research
    def _select(self, scan: ResearchScan, held: dict) -> list[str]:
        ranked = sorted(
            scan.candidates,
            key=lambda item: (item.opportunity_grade.value, item.priority),
        )
        selected = []
        # Existing holdings are reviewed first because a manager may need to
        # close/reduce them; they still receive the same deep report + critique.
        for symbol in held:
            if symbol in self.symbols and symbol not in selected:
                selected.append(symbol)
        for item in ranked:
            if item.symbol in self.symbols and item.symbol not in selected:
                selected.append(item.symbol)
        return selected[: int(self.config.execution.max_deep_candidates)]

    def _research_and_critique(self, run_id, symbol, row, base_payload, memory, deadline):
        self._deadline(deadline, "research " + symbol)
        payload = {
            **{k: v for k, v in base_payload.items() if k not in {"run_id", "universe_version"}},
            "run_id": run_id,
            "universe_version": base_payload.get("universe_version") or base_payload.get("strategy", {}).get("version"),
            "symbol": symbol,
            "focus": row,
            "research_memory": memory.get(symbol, []),
            "deadline_utc": deadline,
        }
        report = self._call(
            role="researcher", mode="RESEARCH_REPORT", payload=payload,
            parser=lambda raw: parse_research_report(
                raw, run_id=run_id, symbol=symbol, now=self.clock()
            ),
            symbol=symbol, report_kind="research",
        )
        report = _with_research_id(report, run_id)
        self._save_report(
            run_id=run_id, role="researcher", symbol=symbol,
            report_id=report.research_report_id, parsed=report,
            meta=payload.get("_meta", {}), status="OK",
        )
        self._deadline(deadline, "skeptic " + symbol)
        critique_payload = {
            "run_id": run_id,
            "as_of_utc": base_payload.get("as_of_utc"),
            "universe_version": payload["universe_version"],
            "symbol": symbol,
            "research_report": report.model_dump(mode="json"),
            "research_report_id": report.research_report_id,
            "focus": {
                key: row.get(key) for key in
                ("symbol", "held", "position", "shortability", "quote")
            },
            "research_memory": memory.get(symbol, []),
            "news": _news_for(base_payload.get("news"), symbol),
            "execution_contract": base_payload.get("execution_contract", {}),
            "deadline_utc": deadline,
        }
        critique = self._call(
            role="skeptic", mode="SKEPTIC_REVIEW", payload=critique_payload,
            parser=lambda raw: parse_critique(
                raw, run_id=run_id, symbol=symbol,
                research_report_id=report.research_report_id, now=self.clock(),
            ),
            symbol=symbol, report_kind="critique",
        )
        critique = _with_critique_id(critique, run_id)
        self._save_report(
            run_id=run_id, role="skeptic", symbol=symbol,
            report_id=critique.critique_id, parsed=critique,
            meta=critique_payload.get("_meta", {}), status="OK",
        )
        self.store.event("agent_critique", {
            "run_id": run_id, "symbol": symbol,
            "severity": critique.critique_severity,
            "assessment": critique.overall_assessment,
        })
        return {
            "report_id": report.research_report_id,
            "critique_id": critique.critique_id,
            "symbol": symbol,
            "report": report.model_dump(mode="json"),
        }, {
            "critique_id": critique.critique_id,
            "report_id": report.research_report_id,
            "symbol": symbol,
            "critique": critique.model_dump(mode="json"),
        }

    # ------------------------------------------------------------------- PM
    def _pm_payload(self, base_payload, run_id, universe_version, focus_rows,
                    reports, critiques, held, blocked, deadline):
        report_by_symbol = {
            item["symbol"]: {**item["report"], "report_id": item["report_id"]}
            for item in reports
        }
        critique_by_symbol = {
            item["symbol"]: {**item["critique"], "critique_id": item["critique_id"]}
            for item in critiques
        }
        positions = {
            str(item.get("symbol")): {
                "direction": item.get("direction"), "qty": item.get("qty"),
                "whole_shares": item.get("whole_shares"), "avg_entry_price": item.get("avg_entry_price"),
                "unrealized_pl": item.get("unrealized_pl"), "stop_status": item.get("stop_status"),
            }
            for item in (base_payload.get("account") or {}).get("positions", [])
            if item.get("symbol")
        }
        focus = []
        for row in focus_rows:
            symbol = row.get("symbol")
            if not symbol:
                continue
            focus.append({
                "symbol": symbol,
                "held": symbol in held,
                "position": positions.get(symbol),
                "quote": row.get("quote"),
                "shortability": row.get("shortability"),
            })
        relevant = sorted(set(report_by_symbol) | set(held) | set(critique_by_symbol))
        memory = self.store.agent_memory(relevant, per_symbol=1)
        feedback = [
            row for row in self.store.agent_feedback(limit=60)
            if row.get("symbol") in relevant
        ]
        return {
            **{k: v for k, v in base_payload.items()
               if k not in {"research", "news", "universe", "focus"}},
            "run_id": run_id,
            "universe_version": universe_version,
            "decision_mode": "THREE_AGENT",
            "focus": focus,
            "research_reports": [
                {**item["report"], "report_id": item["report_id"]} for item in reports
            ],
            "critique_reports": [
                {**item["critique"], "critique_id": item["critique_id"]} for item in critiques
            ],
            "blocked_symbols": sorted(set(blocked)),
            "research_memory": memory,
            "prior_plan_feedback": feedback,
            "deadline_utc": deadline,
        }

    def _validate_plan(self, plan, run_id, base_payload, reports, critiques, blocked):
        if plan.run_id != run_id:
            raise MultiAgentError("PLAN_RUN_ID", "portfolio manager run_id mismatch")
        if plan.decision_mode != "THREE_AGENT":
            raise MultiAgentError("PLAN_MODE", "portfolio manager must use THREE_AGENT mode")
        if plan.market_decision.value != "TRADE" and any(c.order_intent for c in plan.candidates):
            raise MultiAgentError("NON_TRADE_WITH_ORDER", "only a TRADE plan may contain order intents")
        if plan.account_snapshot_id != (base_payload.get("account") or {}).get("account_snapshot_id"):
            raise MultiAgentError("PLAN_SNAPSHOT", "account snapshot mismatch")
        if abs((plan.as_of_utc - self.clock()).total_seconds()) > 900:
            raise MultiAgentError("PLAN_STALE", "portfolio manager plan is stale")
        report_ids = {item["report_id"] for item in reports}
        critique_ids = {item["critique_id"] for item in critiques}
        blocked_set = set(blocked)
        for candidate in plan.candidates:
            if candidate.symbol not in self.symbols:
                raise MultiAgentError("PLAN_OUTSIDE_POOL", candidate.symbol)
            if candidate.order_intent is not None:
                if candidate.symbol in blocked_set:
                    raise MultiAgentError(
                        "INCOMPLETE_AGENT_EVIDENCE",
                        "portfolio manager attempted to trade a blocked symbol: " + candidate.symbol,
                    )
                if candidate.research_report_id not in report_ids or candidate.critique_report_id not in critique_ids:
                    raise MultiAgentError(
                        "INCOMPLETE_AGENT_EVIDENCE",
                        "order_intent lacks a completed research report and skeptic critique: " + candidate.symbol,
                    )
        if plan.market_decision.value == "NO_TRADE" and any(c.order_intent for c in plan.candidates):
            raise MultiAgentError("NO_TRADE_WITH_ORDER", "NO_TRADE plan contains an order")

    # --------------------------------------------------------------- baseline
    def _baseline(self, run_id, base_payload, focus_rows, held, blocked, deadline):
        if not self.config.comparison.enable_single_agent_baseline:
            return None
        if not self.config.agents.baseline.enabled:
            self.store.update_agent_run(run_id, baseline_status="DISABLED")
            return None
        try:
            self._deadline(deadline, "baseline")
            before_tokens = self.budget.usage.tokens
            before_latency = self.budget.usage.latency_ms
            payload = {
                **{k: v for k, v in base_payload.items() if k not in {"run_id", "universe_version"}},
                "run_id": run_id,
                "decision_mode": "SINGLE_AGENT",
                "focus": [
                    {
                        "symbol": row.get("symbol"), "held": row.get("held"),
                        "position": row.get("position"), "quote": row.get("quote"),
                        "shortability": row.get("shortability"),
                        "scan_summary": (row.get("history") or {}).get("daily_recent", [])[-1:]
                        if isinstance(row.get("history"), dict) else [],
                    }
                    for row in focus_rows
                ],
                "baseline_shadow_only": True,
                "held_symbols": sorted(held),
                "blocked_symbols": sorted(set(blocked)),
            }
            plan = self._call(
                role="baseline", mode="PREMARKET_PLAN", payload=payload,
                parser=lambda raw: parse_plan(raw, symbols=self.symbols, now=self.clock()),
                symbol="", report_kind="baseline",
            )
            parsed = plan.model_dump(mode="json")
            self._save_report(
                run_id=run_id, role="baseline", symbol="", report_id=f"baseline-{run_id}",
                parsed=plan, meta=payload.get("_meta", {}), status="OK",
            )
            self.store.update_agent_run(run_id, baseline_status="COMPLETE")
            self.store.event("agent_baseline", {
                "run_id": run_id, "decision": plan.market_decision.value,
                "candidates": [candidate.symbol for candidate in plan.candidates],
                "shadow_only": True,
            })
            return {
                "report_id": f"baseline-{run_id}", "plan": parsed,
                "tokens": self.budget.usage.tokens - before_tokens,
                "latency_ms": self.budget.usage.latency_ms - before_latency,
            }
        except Exception as exc:  # noqa: BLE001 - baseline failure is isolated
            self.store.update_agent_run(run_id, baseline_status="FAILED", error=str(exc)[:500])
            self.store.event("agent_baseline_failed", {
                "run_id": run_id, "reason": type(exc).__name__ + ": " + str(exc)[:300],
            })
            return None

    # --------------------------------------------------------------- helpers
    def _save_report(self, *, run_id, role, symbol, report_id, parsed, meta, status):
        prompt_hash = (meta or {}).get("prompt_hash")
        self.store.save_agent_report({
            "report_id": report_id, "run_id": run_id, "role": role, "symbol": symbol,
            "status": status, "schema_version": getattr(parsed, "schema_version", "1.0"),
            "model": (meta or {}).get("model"), "prompt_hash": prompt_hash,
            "input_hash": (meta or {}).get("input_hash"),
            "output_hash": (meta or {}).get("output_hash"),
            "report_json": (meta or {}).get("raw", ""),
            "parsed_json": parsed.model_dump(mode="json"),
            "error": "",
        })
        self.store.event("agent_report", {
            "run_id": run_id, "role": role, "symbol": symbol,
            "report_id": report_id, "status": status,
        })

    def _finish(self, run_id, status, error):
        self.store.update_agent_run(run_id, status=status, error=error)

    def _record_experiment(self, run_id, trading_day, snapshot_hash, plan, baseline,
                           *, three_tokens=None, three_latency=None):
        self.store.record_agent_experiment({
            "run_id": run_id, "trading_day": trading_day, "snapshot_hash": snapshot_hash,
            "baseline_report_id": (baseline or {}).get("report_id"),
            "three_agent_plan_id": f"pm-{run_id}",
            "baseline_decision": (baseline or {}).get("plan") or {},
            "three_agent_decision": plan.model_dump(mode="json"),
            "baseline_tokens": (baseline or {}).get("tokens"),
            "three_agent_tokens": three_tokens if three_tokens is not None else self.budget.usage.tokens,
            "baseline_latency_ms": (baseline or {}).get("latency_ms"),
            "three_agent_latency_ms": three_latency if three_latency is not None else self.budget.usage.latency_ms,
        })

    def _deadline(self, deadline, stage):
        if deadline is not None and self.clock() >= deadline:
            raise MultiAgentError("DEADLINE", f"research deadline passed before {stage}")


def _empty_plan(base_payload, run_id, decision):
    from .decision_schema import MarketDecision
    return PremarketPlan(
        as_of_utc=datetime.fromisoformat(str(base_payload["as_of_utc"]).replace("Z", "+00:00")),
        model_id="multi-agent",
        market_decision=MarketDecision(decision),
        account_snapshot_id=base_payload["account"]["account_snapshot_id"],
        run_id=run_id,
        decision_mode="THREE_AGENT",
        candidates=[],
    )


def _with_ids(scan: ResearchScan, run_id: str) -> ResearchScan:
    # Schema has no scan id; the report id is deterministic at persistence time.
    return scan


def _with_research_id(report: ResearchReport, run_id: str) -> ResearchReport:
    if report.research_report_id:
        return report
    return report.model_copy(update={"research_report_id": f"rr-{run_id}-{report.symbol}"})


def _with_critique_id(critique: CritiqueReport, run_id: str) -> CritiqueReport:
    if critique.critique_id:
        return critique
    return critique.model_copy(update={"critique_id": f"cr-{run_id}-{critique.symbol}"})


def _with_meta(parsed, meta):
    # The parsed object remains a strict schema; call metadata is returned via
    # the payload's _meta side channel and persisted separately.
    try:
        object.__setattr__(parsed, "_call_meta", dict(meta or {}))
    except Exception:  # noqa: BLE001
        pass
    return parsed


def _jsonable(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


def _news_for(news, symbol):
    if not isinstance(news, dict):
        return []
    items = news.get(symbol)
    return items if isinstance(items, list) else []


def _hash(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:24]


def _estimate_tokens(payload: dict) -> int:
    """Conservative preflight estimate; GPT-style JSON is often ~3-4 chars/token."""
    try:
        raw = json.dumps(payload, default=str, ensure_ascii=False, separators=(",", ":"))
    except Exception:  # noqa: BLE001
        return 0
    return max(1, (len(raw) + 2) // 3)


def _redact(value):
    return redact(value)
