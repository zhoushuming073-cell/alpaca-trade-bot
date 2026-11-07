"""Isolated, offline-only prototype of one-shot AI order decisions.

This module is deliberately NOT registered with the main worker, dashboard,
LumiBot strategy or supervisor.  It refuses a real broker.  Python handles
order legality and state reconciliation; an injected test AI handles decisions.
An accepted DAY order is not strategically reconsidered or canceled by a later
NO_TRADE decision.  After the configured cutoff, the engine is read-only.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal

from pydantic import Field, model_validator

from .decision_schema import OrderIntent, as_order_intent
from .execution import OrderService
from .market_data import session_info
from .order_state import OrderState
from .risk_manager import reserved_buy_cash
from .settings import Strict


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


def parse_one_shot_decision(raw: str) -> OneShotDecision:
    return OneShotDecision.model_validate_json(raw)


def _fingerprint(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:32]


def _account_identity(account: dict) -> str:
    """Exclude volatile quote/time fields, but include exposure and open orders."""
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
        "equity": account.get("equity"),
        "quantities": account.get("quantities", {}),
        "open_orders": sorted(orders, key=lambda row: str(row.get("client_order_id"))),
        "blocked": account.get("blocked"),
    })


def _same_pending_intent(order: dict, intent: dict) -> bool:
    try:
        return (
            str(order.get("symbol")) == intent["symbol"]
            and str(order.get("side") or "").lower() == intent["side"]
            and str(order.get("position_intent") or (
                "buy_to_open" if intent["side"] == "buy" else "sell_to_close"
            )).lower() == intent["position_intent"]
            and Decimal(str(order.get("qty"))) == Decimal(str(intent["qty"]))
            and Decimal(str(order.get("limit_price"))) == Decimal(str(intent["limit_price"]))
        )
    except (ValueError, TypeError, ArithmeticError):
        return False


class OneShotEngine:
    """Offline orchestrator; it cannot be activated by the production entrypoint.

    ``cycle`` may request a validated decision from an injected synthetic AI.
    ``apply_decision`` also supports deterministic scenario tests.  Neither
    method retries an accepted order or evaluates an outstanding order's thesis.
    """

    def __init__(self, store, broker, ai, config, *, clock=None, context_provider=None,
                 max_snapshot_age_seconds: int = 90):
        from .dry_run import FakeBroker

        if not isinstance(broker, FakeBroker):
            raise RuntimeError("OneShotEngine is an OFFLINE-ONLY prototype: FakeBroker required")
        self.store = store
        self.broker = broker
        self.ai = ai
        self.config = config
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.context_provider = context_provider
        self.max_snapshot_age_seconds = max_snapshot_age_seconds
        self.orders = OrderService(store, broker, config.execution, clock=self.clock)

    def context(self) -> dict:
        """Read-only reconciliation, followed by a broker-authoritative snapshot."""
        self.orders.reconcile()  # Never cancel lingering DAY orders.
        account = self.broker.snapshot()
        session = session_info(self.clock(), self.config.execution)
        reserved = reserved_buy_cash(account, self.store.unresolved_orders())
        context = {
            "as_of_utc": self.clock().astimezone(timezone.utc).isoformat(),
            "snapshot_id": _account_identity(account),
            "session": {
                "phase": session.phase.value,
                "now_et": session.now_et.isoformat(),
                "can_submit": session.can_submit,
                "submit_cutoff": session.submit_cutoff.isoformat() if session.submit_cutoff else None,
            },
            "account": {
                "account_id_hash": account.get("account_id_hash"),
                "cash": account.get("cash"),
                "equity": account.get("equity"),
                "buying_power": account.get("buying_power"),
                "non_margin_cash": account.get("non_margin_cash"),
                "reserved_buy_cash": reserved,
                "available_non_margin_cash": max(0.0, float(account.get("cash") or 0) - reserved),
                "positions": account.get("position_details") or {},
                "quantities": account.get("quantities") or {},
                "open_orders": account.get("open_orders") or [],
            },
            "allowed_symbols": list(self.config.symbols),
            "execution_contract": {
                "mode": "Paper only (offline prototype)",
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
            "recent_decisions": [
                row["payload"] for row in self.store.recent_events(("one_shot_decision",), 12)
            ],
            "research": {"status": "UNAVAILABLE", "reason": "No read-only research provider configured"},
        }
        # After 12:00 the local loop does not even refresh research; it only
        # reconciles brokerage orders and observes account/position changes.
        if self.context_provider and (session.premarket or session.active):
            context["research"] = self.context_provider(context)
        return context

    def cycle(self) -> dict:
        context = self.context()
        if not session_info(self.clock(), self.config.execution).can_submit:
            return {"status": "OBSERVE_ONLY", "context": context}
        if not self.ai:
            return {"status": "NO_AI", "context": context}
        try:
            decision, _meta = self.ai.complete(
                "ONE_SHOT_DECISION", context, parse_one_shot_decision
            )
            return self.apply_decision(decision, context)
        except Exception as exc:  # noqa: BLE001 - never trade on invalid AI output
            self.store.event("one_shot_error", {"stage": "ai_decision", "type": type(exc).__name__})
            return {"status": "AI_FAILED", "reason": type(exc).__name__}

    def apply_decision(self, decision: OneShotDecision, context: dict) -> dict:
        """A single strategy decision becomes at most one new broker POST."""
        decision = OneShotDecision.model_validate(decision)
        session = session_info(self.clock(), self.config.execution)
        if not session.can_submit:
            return {"status": "OBSERVE_ONLY", "reason": "OUTSIDE_SUBMISSION_WINDOW"}
        if self.store.paused() or self.store.freeze_reason() or self.store.config_pending():
            return {"status": "BLOCKED", "reason": "PAUSED_OR_FROZEN_OR_CONFIG_PENDING"}
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
        fingerprint = _fingerprint({
            "action": decision.action, "intent": intent,
            "cancel_conflicting_orders": decision.cancel_conflicting_orders,
            "intentional_additional_pending": decision.intentional_additional_pending,
        })
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
            result = self._cancel_conflicting(opposing, account, intent["symbol"])
            if result:
                return result
            account = self.broker.snapshot()
            pending = list(account.get("open_orders") or [])

        if not decision.intentional_additional_pending and any(
            _same_pending_intent(order, intent) for order in pending
        ):
            return {"status": "BLOCKED", "reason": "DUPLICATE_PENDING_INTENT"}

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

    def _cancel_conflicting(self, opposing: list[dict], account: dict, symbol: str) -> dict | None:
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
                self.broker.cancel(cid)
                observed = self.broker.lookup(cid)
            except Exception as exc:  # noqa: BLE001 - unknown cancel result
                self.store.block("CANCEL_OUTCOME_UNKNOWN: " + cid)
                self.store.event("one_shot_cancel_unknown", {"cid": cid, "type": type(exc).__name__})
                return {"status": "BLOCKED", "reason": "CANCEL_OUTCOME_UNKNOWN"}
            if observed:
                self.store.apply_broker_order(cid, observed)
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
