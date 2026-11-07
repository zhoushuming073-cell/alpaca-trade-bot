"""The sole trading decision engine for the Alpaca Paper worker.

AI owns strategy; this engine owns state freshness, liveness and dispatch
coordination. The existing OrderService remains the only broker POST path.
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal

from pydantic import Field, model_validator
import portalocker

from .decision_schema import OrderIntent, as_order_intent
from .execution import OrderService
from .market_data import SessionPhase, session_info
from .order_state import OrderState
from .risk_manager import reserved_buy_cash
from .settings import Strict, config_snapshot, load_config


class OneShotDecision(Strict):
    """A strategy choice, not permission to bypass the execution contract."""

    schema_version: Literal["1.0"] = "1.0"
    mode: Literal["ONE_SHOT_DECISION"] = "ONE_SHOT_DECISION"
    decision_id: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")
    snapshot_id: str = Field(min_length=16, max_length=64)
    action: Literal["NO_TRADE", "PLACE_ORDER"]
    reason: str = Field(min_length=1, max_length=4000)
    order_intent: OrderIntent | None = None
    cancel_conflicting_orders: bool = False
    intentional_additional_pending: bool = False
    research_symbols: list[str] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def _consistent(self):
        if self.action == "NO_TRADE":
            if self.order_intent is not None or self.cancel_conflicting_orders or self.intentional_additional_pending:
                raise ValueError("NO_TRADE cannot submit, replace or cancel an order")
        elif self.order_intent is None:
            raise ValueError("PLACE_ORDER requires a complete order_intent")
        if self.cancel_conflicting_orders and self.order_intent.position_intent not in {
            "sell_to_close", "buy_to_close",
        }:
            raise ValueError("Only a position-closing decision can cancel conflicting orders")
        return self


class OneShotResearch(Strict):
    schema_version: Literal["1.0"] = "1.0"
    mode: Literal["ONE_SHOT_RESEARCH"] = "ONE_SHOT_RESEARCH"
    snapshot_id: str = Field(min_length=16, max_length=64)
    summary: str = Field(min_length=1, max_length=6000)
    focus_symbols: list[str] = Field(default_factory=list, max_length=8)
    needs_deeper_research: bool = False

    @model_validator(mode="after")
    def _research_request_has_symbols(self):
        if self.needs_deeper_research and not self.focus_symbols:
            raise ValueError("Deeper research requires focus_symbols")
        return self


def parse_one_shot_research(raw: str) -> OneShotResearch:
    return OneShotResearch.model_validate_json(raw)


def parse_one_shot_decision(raw: str) -> OneShotDecision:
    return OneShotDecision.model_validate_json(raw)


def _fingerprint(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:32]


def decision_fingerprint(decision: OneShotDecision) -> str:
    intent = as_order_intent(decision.order_intent) if decision.order_intent else None
    return _fingerprint({
        "action": decision.action, "intent": intent,
        "cancel_conflicting_orders": decision.cancel_conflicting_orders,
        "intentional_additional_pending": decision.intentional_additional_pending,
    })


def _account_identity(account: dict) -> str:
    """Bind structure and spendable funds; let mark-to-market equity float.

    Equity is rechecked against the daily loss limit by OrderService's final
    broker snapshot.  Cash and buying power are never given a tolerance here.
    """
    orders = [
        {k: item.get(k) for k in (
            "client_order_id", "symbol", "side", "position_intent", "qty",
            "filled_qty", "limit_price", "status",
        )}
        for item in account.get("open_orders", []) or []
    ]
    return _fingerprint({
        "account_id_hash": account.get("account_id_hash"),
        "cash": account.get("cash"),
        "non_margin_cash": account.get("non_margin_cash"),
        "buying_power": account.get("buying_power"),
        "quantities": account.get("quantities", {}),
        "available_quantities": account.get("available_quantities", {}),
        "open_orders": sorted(orders, key=lambda row: str(row.get("client_order_id"))),
        "blocked": account.get("blocked"),
        "shorting_enabled": account.get("shorting_enabled"),
    })


def _same_pending_intent(order: dict, intent: dict) -> bool:
    try:
        return (
            str(order.get("symbol")) == intent["symbol"]
            and str(getattr(order.get("side"), "value", order.get("side")) or "").lower() == intent["side"]
            and str(getattr(order.get("position_intent"), "value", order.get("position_intent")) or (
                "buy_to_open" if intent["side"] == "buy" else "sell_to_close"
            )).lower() == intent["position_intent"]
            and Decimal(str(order.get("qty"))) == Decimal(str(intent["qty"]))
            and Decimal(str(order.get("limit_price"))) == Decimal(str(intent["limit_price"]))
        )
    except (ValueError, TypeError, ArithmeticError):
        return False


def _same_pending_direction(order: dict, intent: dict) -> bool:
    """Identify live same-side exposure regardless of price or quantity."""
    if str(order.get("status") or "").lower() in {
        "filled", "canceled", "cancelled", "expired", "rejected", "replaced",
    }:
        return False
    return (
        str(order.get("symbol") or "") == intent["symbol"]
        and str(getattr(order.get("side"), "value", order.get("side")) or "").lower() == intent["side"]
        and str(getattr(order.get("position_intent"), "value", order.get("position_intent")) or (
            "buy_to_open" if intent["side"] == "buy" else "sell_to_close"
        )).lower() == intent["position_intent"]
    )


def _order_context(order: dict, account_asof: str | None) -> dict:
    item = dict(order)
    try:
        item["remaining_qty"] = str(max(
            Decimal("0"), Decimal(str(item["qty"])) - Decimal(str(item.get("filled_qty") or 0))
        ))
    except (KeyError, ValueError, ArithmeticError):
        item["remaining_qty"] = None
    item["observed_at_utc"] = account_asof
    return item


class OneShotEngine:
    """One decision can produce at most one durable OrderService dispatch."""

    def __init__(self, store, broker, ai, config, *, clock=None, context_provider=None,
                 max_snapshot_age_seconds: int = 90, require_watchdog=None,
                 check_disk_config=None):
        from .dry_run import FakeBroker
        from .broker import PaperGateway

        if not isinstance(broker, (FakeBroker, PaperGateway)):
            raise RuntimeError("OneShotEngine requires FakeBroker or Alpaca PaperGateway")
        self.paper = isinstance(broker, PaperGateway)
        self.store = store
        self.broker = broker
        self.ai = ai
        self.config = config
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.context_provider = context_provider
        self.max_snapshot_age_seconds = max_snapshot_age_seconds
        self.require_watchdog = self.paper if require_watchdog is None else require_watchdog
        self.check_disk_config = self.paper if check_disk_config is None else check_disk_config
        self.started_at = self.clock()
        self.tick_lock = threading.RLock()
        self._last_decision_at = None
        self._last_attempt_at = None
        self._decision_day = None
        self._consecutive_ai_failures = 0
        self._next_retry_at = None
        self._premarket_day = None
        self._last_account_event = None
        self.orders = OrderService(store, broker, config.execution, clock=self.clock,
                                   require_web_heartbeat=self.paper)
        if store.active_config() is None:
            store.set_active_config(config_snapshot(config))
        recovered = store.recover_stale_leases()
        if recovered:
            store.event("order_recovery", {"count": len(recovered), "states": "NOT_SENT"})

    def _decision_gate(self) -> str | None:
        if self.store.paused() or self.store.freeze_reason() or self.store.config_pending():
            return "paused_or_frozen"
        if self.check_disk_config:
            try:
                if load_config().hash != self.config.hash:
                    self.store.set_config_pending(True)
                    return "config_pending"
            except Exception as exc:
                self.store.set_config_pending(True)
                self.store.event("config_load", {"error": type(exc).__name__})
                return "config_pending"
        if self.paper:
            if not self.store.web_connected(30):
                return "web_disconnected"
            if self.store.web_reconnect_required():
                result = self.orders.reconcile(cancel_lingering=False)
                if any(result.get(key) for key in ("errors", "unknown", "conflicts")):
                    return "web_reconcile_incomplete"
                self.store.set_web_reconnect_required(False)
        if self.require_watchdog:
            health = self.store.health("watchdog", self.config.execution.watchdog_stale_seconds)
            if health["consecutive_failures"] >= 3:
                self.store.block("WATCHDOG_UNHEALTHY: repeated reconciliation failures")
                return "watchdog_unhealthy"
            if not health["alive"] or health["stale"] or not health["last_success"]:
                return "watchdog_unavailable"
            last = datetime.fromisoformat(health["last_success"].replace("Z", "+00:00"))
            if (self.clock() - last).total_seconds() > max(120, 3 * self.config.execution.watchdog_stale_seconds):
                return "watchdog_stale"
        return None

    def tick(self) -> dict:
        """LumiBot callback; broker reconciliation remains active after noon."""
        with self.tick_lock:
            self.store.heartbeat("worker", ok=True, detail={"phase": "one_shot_tick"})
            session = session_info(self.clock(), self.config.execution)
            if session.phase is SessionPhase.NO_SESSION:
                return {"status": "CLOSED"}
            attempt_claimed = False
            try:
                if session.premarket:
                    return self.premarket_tick()
                if not session.can_submit:
                    return {"status": "OBSERVE_ONLY", "context": self.context()}
                gate = self._decision_gate()
                if gate:
                    self.orders.reconcile(cancel_lingering=False)
                    return {"status": "BLOCKED", "reason": gate}
                if not self.ai:
                    self.orders.reconcile(cancel_lingering=False)
                    return {"status": "NO_AI"}
                self._load_decision_clock(session.trading_day)
                now = self.clock()
                if self._next_retry_at and now < self._next_retry_at:
                    self.orders.reconcile(cancel_lingering=False)
                    return {"status": "RETRY_BACKOFF", "next_retry_at": self._next_retry_at.isoformat()}
                if self._last_decision_at and (self.clock() - self._last_decision_at).total_seconds() < self.config.execution.decision_interval_seconds:
                    self.orders.reconcile(cancel_lingering=False)
                    return {"status": "WAITING_NEXT_RESEARCH"}
                attempt = self.store.claim_one_shot_ai_attempt(
                    session.trading_day, self.config.execution.max_decision_attempts_per_day,
                )
                if attempt is None:
                    self.orders.reconcile(cancel_lingering=False)
                    return {"status": "BLOCKED", "reason": "AI_ATTEMPT_LIMIT"}
                attempt_claimed = True
                self._last_attempt_at = now
                self.store.event("one_shot_ai_attempt", {"trading_day": session.trading_day,
                                                         "attempt": attempt, "at_utc": now.isoformat()})
                result = self.cycle()
                if result.get("status") in {"NO_TRADE", "ORDER_RESULT"}:
                    self._last_decision_at = self.clock()
                    self.store.set_control("one_shot_last_completed_at:" + session.trading_day,
                                           self._last_decision_at.isoformat())
                    self._consecutive_ai_failures = 0
                    self._next_retry_at = None
                    self.store.set_control("one_shot_ai_failures:" + session.trading_day, "0")
                    self.store.set_control("one_shot_next_retry_at:" + session.trading_day, "")
                elif result.get("status") == "AI_FAILED":
                    self._schedule_decision_retry(session.trading_day, "AI_FAILED", failure=True)
                elif result.get("status") in {"RESEARCH_REQUESTED", "BLOCKED"}:
                    self._schedule_decision_retry(session.trading_day, result["status"], failure=False)
                return result
            except Exception as exc:
                self.store.error("one_shot_tick", exc, outcome="cycle_failed")
                if session.can_submit and attempt_claimed:
                    try:
                        self._schedule_decision_retry(session.trading_day, "DATA_OR_CYCLE_FAILED", failure=False)
                    except Exception as schedule_error:
                        self.store.error("one_shot_retry", schedule_error, outcome="retry_state_unavailable")
                return {"status": "FAILED", "reason": type(exc).__name__}

    def _load_decision_clock(self, trading_day: str) -> None:
        if self._decision_day == trading_day:
            return
        self._decision_day = trading_day
        completed = self.store.get_control("one_shot_last_completed_at:" + trading_day)
        retry_at = self.store.get_control("one_shot_next_retry_at:" + trading_day)
        self._last_decision_at = datetime.fromisoformat(completed) if completed else None
        self._next_retry_at = datetime.fromisoformat(retry_at) if retry_at else None
        self._consecutive_ai_failures = int(
            self.store.get_control("one_shot_ai_failures:" + trading_day) or 0
        )
        self._last_attempt_at = None

    def _schedule_decision_retry(self, trading_day: str, reason: str, *, failure: bool) -> None:
        if failure:
            self._consecutive_ai_failures += 1
        delays = self.config.execution.ai_retry_backoff_seconds
        index = min(max(self._consecutive_ai_failures - 1, 0), len(delays) - 1)
        self._next_retry_at = self.clock() + timedelta(seconds=delays[index])
        self.store.set_control("one_shot_ai_failures:" + trading_day,
                               str(self._consecutive_ai_failures))
        self.store.set_control("one_shot_next_retry_at:" + trading_day,
                               self._next_retry_at.isoformat())
        self.store.event("one_shot_retry", {
            "trading_day": trading_day, "reason": reason,
            "consecutive_ai_failures": self._consecutive_ai_failures,
            "next_retry_at": self._next_retry_at.isoformat(),
        })

    def premarket_tick(self) -> dict:
        with self.tick_lock:
            session = session_info(self.clock(), self.config.execution)
            if not session.premarket:
                return {"status": "OUTSIDE_PREMARKET"}
            gate = self._decision_gate()
            if gate:
                return {"status": "BLOCKED", "reason": gate}
            if self._premarket_day == session.trading_day:
                return {"status": "PREMARKET_PREPARED"}
            context = self.context()
            if not self.ai:
                return {"status": "NO_AI"}
            research, _meta = self.ai.complete(
                "ONE_SHOT_RESEARCH", context, parse_one_shot_research
            )
            if research.snapshot_id != context["snapshot_id"] or any(
                symbol not in self.config.symbols for symbol in research.focus_symbols
            ):
                raise ValueError("Invalid premarket research binding")
            detail = research.model_dump(mode="json")
            detail["as_of_utc"] = context["as_of_utc"]
            self.store.event("one_shot_research", detail)
            if research.needs_deeper_research and self.context_provider:
                deeper = self.context_provider({**context, "focus_symbols": research.focus_symbols})
                self.store.event("one_shot_research_request", {
                    "as_of_utc": context["as_of_utc"], "symbols": research.focus_symbols,
                    "result": deeper,
                })
            self._premarket_day = session.trading_day
            self.store.event("one_shot_premarket", {"as_of_utc": context["as_of_utc"],
                                                     "research": context["research"].get("status")})
            return {"status": "PREMARKET_PREPARED"}

    def context(self) -> dict:
        """Read-only reconciliation, followed by a broker-authoritative snapshot."""
        result = self.orders.reconcile(cancel_lingering=False)
        if any(result.get(key) for key in ("errors", "unknown", "conflicts")):
            raise RuntimeError("Broker reconciliation incomplete")
        account = self.broker.snapshot(include_history=True)
        history_degraded = bool(self.paper and account.get("recent_orders_error"))
        quality = "DEGRADED" if history_degraded else "COMPLETE"
        if self.paper and quality != self._last_account_event:
            self.store.event("one_shot_data_quality", {
                "at_utc": self.clock().astimezone(timezone.utc).isoformat(),
                "recent_closed_orders": quality,
                "reason": "closed_order_history_unavailable" if history_degraded else "restored",
            })
            self._last_account_event = quality
        session = session_info(self.clock(), self.config.execution)
        reserved = reserved_buy_cash(account, self.store.unresolved_orders())
        context = {
            "as_of_utc": self.clock().astimezone(timezone.utc).isoformat(),
            "account_as_of_utc": account.get("asof"),
            "snapshot_id": _account_identity(account),
            "data_quality": {
                "critical_broker_state": "COMPLETE",
                "recent_closed_orders": quality,
                "note": "Recent closed orders are unknown" if history_degraded else "Broker history available",
            },
            "session": {
                "phase": session.phase.value,
                "now_et": session.now_et.isoformat(),
                "can_submit": session.can_submit,
                "submit_cutoff": session.submit_cutoff.isoformat() if session.submit_cutoff else None,
            },
            "account": {
                "as_of_utc": account.get("asof"),
                "account_id_hash": account.get("account_id_hash"),
                "cash": account.get("cash"),
                "equity": account.get("equity"),
                "buying_power": account.get("buying_power"),
                "non_margin_cash": account.get("non_margin_cash"),
                "reserved_buy_cash": reserved,
                "available_non_margin_cash": max(0.0, float(account.get("cash") or 0) - reserved),
                "positions": account.get("position_details") or {},
                "quantities": account.get("quantities") or {},
                "open_orders": [_order_context(item, account.get("asof"))
                                for item in account.get("open_orders") or []],
                "recent_orders": [_order_context(item, account.get("asof"))
                                  for item in account.get("recent_orders") or []],
                "recent_orders_error": account.get("recent_orders_error"),
            },
            "allowed_symbols": list(self.config.symbols),
            "execution_contract": {
                "mode": "Alpaca Paper" if self.paper else "synthetic offline",
                "order_type": "limit",
                "time_in_force": "day",
                "whole_shares": True,
                "extended_hours": False,
                "shorting_allowed": bool(self.config.execution.allow_shorting),
                "broker_stop_verified": bool(
                    self.config.execution.allow_broker_stop
                    and self.config.execution.broker_stop_verified
                ),
            },
            "strategy_guidance": {
                "entry": self.config.policy.entry_rules,
                "exit": self.config.policy.exit_rules,
                "analysis": self.config.policy.analysis_rules,
                "authority": "advisory, not mandatory trade filters",
            },
            "recent_decisions": self.store.recent_events(("one_shot_decision",), 12),
            "recent_order_results": self.store.recent_events(("one_shot_result",), 12),
            "recent_research": self.store.recent_events(("one_shot_research",), 4),
            "recent_focused_research": self.store.recent_events(("one_shot_research_request",), 2),
            "research": {"status": "UNAVAILABLE", "reason": "No read-only research provider configured"},
        }
        # After 12:00 the local loop does not even refresh research; it only
        # reconciles brokerage orders and observes account/position changes.
        if self.context_provider and (session.premarket or session.active):
            context["research"] = self.context_provider(context)
        if self.paper:
            asof = datetime.fromisoformat(str(account.get("asof") or "").replace("Z", "+00:00"))
            age = (self.clock() - asof).total_seconds()
            if not 0 <= age <= self.max_snapshot_age_seconds:
                raise RuntimeError("Broker account snapshot is stale")
        return context

    def cycle(self) -> dict:
        if self.paper:
            gate = self._decision_gate()
            if gate:
                return {"status": "BLOCKED", "reason": gate}
        context = self.context()
        if not session_info(self.clock(), self.config.execution).can_submit:
            return {"status": "OBSERVE_ONLY", "context": context}
        if not self.ai:
            return {"status": "NO_AI", "context": context}
        try:
            decision, _meta = self.ai.complete(
                "ONE_SHOT_DECISION", context, parse_one_shot_decision
            )
            if any(symbol not in self.config.symbols for symbol in decision.research_symbols):
                return {"status": "AI_FAILED", "reason": "OUTSIDE_RESEARCH_POOL"}
            if decision.research_symbols and self.context_provider:
                deeper = self.context_provider({**context, "focus_symbols": decision.research_symbols})
                self.store.event("one_shot_research_request", {
                    "as_of_utc": context["as_of_utc"], "symbols": decision.research_symbols,
                    "result": deeper,
                })
                if decision.action == "PLACE_ORDER":
                    return {"status": "RESEARCH_REQUESTED", "reason": "NEW_RESEARCH_REQUIRES_NEW_DECISION"}
            return self.apply_decision(decision, context)
        except Exception as exc:  # noqa: BLE001 - never trade on invalid AI output
            self.store.event("one_shot_error", {"stage": "ai_decision", "type": type(exc).__name__})
            return {"status": "AI_FAILED", "reason": type(exc).__name__}

    def apply_decision(self, decision: OneShotDecision, context: dict) -> dict:
        """Serialize different decision IDs against one Paper account."""
        lock_path = str(self.store.path) + ".one_shot_dispatch.lock"
        try:
            with portalocker.Lock(lock_path, timeout=0):
                return self._apply_decision_locked(decision, context)
        except portalocker.LockException:
            return {"status": "BLOCKED", "reason": "CONCURRENT_DECISION"}

    def _apply_decision_locked(self, decision: OneShotDecision, context: dict) -> dict:
        """A single strategy decision becomes at most one new broker POST."""
        decision = OneShotDecision.model_validate(decision)
        session = session_info(self.clock(), self.config.execution)
        if not session.can_submit:
            return {"status": "OBSERVE_ONLY", "reason": "OUTSIDE_SUBMISSION_WINDOW"}
        if self.store.paused() or self.store.freeze_reason() or self.store.config_pending():
            return {"status": "BLOCKED", "reason": "PAUSED_OR_FROZEN_OR_CONFIG_PENDING"}
        if self.paper:
            gate = self._decision_gate()
            if gate:
                return {"status": "BLOCKED", "reason": gate}
        try:
            as_of = datetime.fromisoformat(context["as_of_utc"])
            age = (self.clock().astimezone(timezone.utc) - as_of.astimezone(timezone.utc)).total_seconds()
        except (ValueError, TypeError, KeyError, AttributeError):
            return {"status": "BLOCKED", "reason": "INVALID_SNAPSHOT_TIME"}
        if age < 0 or age > self.max_snapshot_age_seconds:
            return {"status": "BLOCKED", "reason": "STALE_DECISION_CONTEXT"}
        if decision.snapshot_id != context.get("snapshot_id"):
            return {"status": "BLOCKED", "reason": "SNAPSHOT_ID_MISMATCH"}
        if _account_identity(self.broker.snapshot()) != context["snapshot_id"]:
            return {"status": "BLOCKED", "reason": "ACCOUNT_CHANGED_SINCE_AI_DECISION"}

        intent = as_order_intent(decision.order_intent) if decision.order_intent else None
        fingerprint = decision_fingerprint(decision)
        reservation = self.store.reserve_one_shot_decision(
            decision.decision_id, fingerprint, decision.action,
        )
        if reservation == "CONFLICT":
            return {"status": "BLOCKED", "reason": "DECISION_ID_CONFLICT"}
        if reservation == "DUPLICATE":
            cid = self.orders.client_order_id(decision.decision_id, "immediate", intent) if intent else None
            row = self.store.order_row(cid) if cid else None
            return {"status": "DUPLICATE", **({"cid": cid, "order_state": row["state"]} if row else {})}

        self.store.event("one_shot_decision", {
            "decision_id": decision.decision_id,
            "snapshot_id": decision.snapshot_id,
            "action": decision.action,
            "reason": decision.reason,
            "order_intent": decision.order_intent.model_dump(mode="json") if decision.order_intent else None,
            "cancel_conflicting_orders": decision.cancel_conflicting_orders,
            "intentional_additional_pending": decision.intentional_additional_pending,
        })
        if decision.action == "NO_TRADE":
            return {"status": "NO_TRADE"}  # Does not cancel accepted DAY orders.

        cid = self.orders.client_order_id(decision.decision_id, "immediate", intent)
        # OrderService's CID intentionally includes symbol/side but not price.
        # Reusing a decision_id for a different symbol/direction must not be
        # interpreted as permission to POST a second, distinct CID.
        with self.store.lock:
            previous = self.store.db.execute(
                "SELECT cid,intent FROM orders WHERE plan_id=? AND trigger_id='immediate'",
                (decision.decision_id,),
            ).fetchall()
        for previous_order in previous:
            try:
                previous_intent = json.loads(previous_order["intent"])
            except (ValueError, TypeError):
                return {"status": "BLOCKED", "reason": "CORRUPT_PREVIOUS_INTENT"}
            if _fingerprint(previous_intent) != _fingerprint(intent):
                return {"status": "BLOCKED", "reason": "DECISION_ID_CONFLICT"}
        existing = self.store.order_row(cid)
        if existing:
            try:
                old_intent = json.loads(existing["intent"])
            except (ValueError, TypeError):
                return {"status": "BLOCKED", "reason": "CORRUPT_PREVIOUS_INTENT"}
            if _fingerprint(old_intent) != _fingerprint(intent):
                return {"status": "BLOCKED", "reason": "DECISION_ID_CONFLICT"}
            return {"status": "DUPLICATE", "cid": cid, "order_state": existing["state"]}

        account = self.broker.snapshot()
        pending = list(account.get("open_orders") or [])
        opposing = [
            item for item in pending
            if item.get("symbol") == intent["symbol"]
            and item.get("side") == (
                "buy" if intent["position_intent"] == "sell_to_close" else "sell"
            )
        ] if intent["position_intent"] in {"sell_to_close", "buy_to_close"} else []
        if opposing and decision.cancel_conflicting_orders:
            held = float((account.get("quantities") or {}).get(intent["symbol"], 0))
            if intent["position_intent"] == "sell_to_close":
                available_position = max(0.0, held)
            else:
                available_position = max(0.0, -held)
            if float(intent["qty"]) > available_position:
                # A zero/insufficient position must never authorize canceling
                # an otherwise valid pending entry as a side effect.
                return {"status": "BLOCKED", "reason": "INSUFFICIENT_POSITION_FOR_EXIT"}
            result = self._cancel_conflicting(opposing, account, intent["symbol"], decision)
            if result:
                return result
            account = self.broker.snapshot()
            pending = list(account.get("open_orders") or [])

        if not decision.intentional_additional_pending:
            same_direction = [order for order in pending if _same_pending_direction(order, intent)]
            if any(_same_pending_intent(order, intent) for order in same_direction):
                return {"status": "BLOCKED", "reason": "DUPLICATE_PENDING_INTENT"}
            if same_direction:
                return {"status": "BLOCKED", "reason": "ADDITIONAL_PENDING_INTENT_REQUIRES_AUTHORIZATION"}

        # This is the only path that can dispatch.  It performs its own fresh
        # broker/account/quote/risk/session checks before the durable POST.
        current_session = session_info(self.clock(), self.config.execution)
        if not current_session.can_submit:
            return {"status": "OBSERVE_ONLY", "reason": "CUTOFF_DURING_DECISION"}
        result = self.orders.submit(
            intent, plan_id=decision.decision_id, trigger_id="immediate",
            day=current_session.trading_day, session=current_session,
            account=account, quote=self.broker.quote(intent["symbol"], self.config.risk),
            symbols=self.config.symbols, risk=self.config.risk,
            buy_attempts=self.store.count_order_attempts(current_session.trading_day),
            freeze_reason=self.store.freeze_reason(),
        )
        self.store.event("one_shot_result", {
            "decision_id": decision.decision_id, "cid": result.get("cid"),
            "state": result.get("state"), "rejected": result.get("rejected"),
        })
        return {"status": "ORDER_RESULT", **result}

    def _cancel_conflicting(self, opposing: list[dict], account: dict, symbol: str,
                            decision: OneShotDecision) -> dict | None:
        """Explicit position-exit prerequisite; never a strategy recheck."""
        before_qty = float((account.get("quantities") or {}).get(symbol, 0))
        for item in opposing:
            cid = str(item.get("client_order_id") or "")
            if not cid or self.store.order_row(cid) is None:
                return {"status": "BLOCKED", "reason": "UNTRACKED_CONFLICTING_ORDER"}
            # A cancellation is a state-changing broker action.  Check the
            # cutoff again immediately before every cancellation request.
            if not session_info(self.clock(), self.config.execution).can_submit:
                return {"status": "OBSERVE_ONLY", "reason": "CUTOFF_BEFORE_CANCEL"}
            try:
                if self.paper:
                    self.broker.cancel(cid, decision=decision)
                else:
                    self.broker.cancel(cid)
                observed = self.broker.lookup(cid)
            except Exception as exc:  # noqa: BLE001 - unknown cancel result
                self.store.block("CANCEL_OUTCOME_UNKNOWN: " + cid)
                self.store.event("one_shot_cancel_unknown", {"cid": cid, "type": type(exc).__name__})
                return {"status": "BLOCKED", "reason": "CANCEL_OUTCOME_UNKNOWN"}
            if observed:
                self.store.apply_broker_order(cid, observed)
                if Decimal(str(observed.get("filled_qty") or 0)) != Decimal(str(item.get("filled_qty") or 0)):
                    return {"status": "BLOCKED", "reason": "FILL_CHANGED_DURING_CANCEL"}
            if not observed or str(observed.get("status") or "").lower() not in {
                "canceled", "cancelled", "expired", "rejected",
            }:
                return {"status": "BLOCKED", "reason": "CANCEL_NOT_CONFIRMED"}
        after = self.broker.snapshot()
        if float((after.get("quantities") or {}).get(symbol, 0)) != before_qty:
            return {"status": "BLOCKED", "reason": "POSITION_CHANGED_DURING_CANCEL"}
        if any(item.get("symbol") == symbol and item.get("side") == opposing[0].get("side")
               for item in after.get("open_orders", [])):
            return {"status": "BLOCKED", "reason": "CONFLICTING_ORDER_STILL_OPEN"}
        return None
