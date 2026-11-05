"""Order submission service and conservative reconciliation.

The lifecycle is split by the durable dispatch boundary in :mod:`order_state`.
Before the boundary, every failure is a deterministic ``NOT_SENT`` outcome and
no broker lookup is performed.  After the boundary, the only safe assumption is
that a POST may have reached the broker: reconcile by ``client_order_id`` and
never re-submit, never convert a 404 into "not sent".
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone

from .order_state import OrderState, is_terminal
from .risk_manager import RiskRejected, validate_new_intent
from .safety import BrokerStopUnsupported, OrderValidationError, PreDispatchRejected


class OrderService:
    def __init__(self, store, broker, execution, clock=None):
        self.store = store
        self.broker = broker
        self.execution = execution
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def client_order_id(plan_id: str, trigger_id: str, intent: dict, attempt: int = 0) -> str:
        raw = f"aip2:{plan_id}:{trigger_id}:{intent.get('symbol')}:{intent.get('side')}:{attempt}"
        return "aip-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:36]

    def _existing_result(self, cid: str) -> dict | None:
        row = self.store.order_row(cid)
        if not row:
            return None
        return {
            "cid": cid,
            "state": row["state"],
            "status": row["status"],
            "duplicate": True,
            "reason": row["reason"],
        }

    def submit(
        self,
        intent: dict,
        *,
        plan_id: str,
        trigger_id: str,
        day: str,
        session,
        account: dict,
        quote: dict,
        symbols,
        risk,
        buy_attempts: int,
        freeze_reason: str = "",
        attempt: int = 0,
    ) -> dict:
        cid = self.client_order_id(plan_id, trigger_id, intent, attempt)
        existing = self._existing_result(cid)
        if existing:
            return existing
        owner = f"{self.store.instance_id}:{uuid.uuid4().hex}"
        self.store.reserve_order(
            cid, day, intent,
            plan_id=plan_id, trigger_id=trigger_id,
            stop_policy=str(intent.get("stop_policy") or "NONE").upper(),
            owner=owner,
        )
        if not self.store.claim_order(cid, owner):
            return self._existing_result(cid) or {"cid": cid, "state": "UNKNOWN", "duplicate": True}
        # Only pre-dispatch failures are handled here.
        try:
            fresh = self.broker.prepare(intent, cid, session=session, account=account)
            # A trigger quote can age while slow preflight checks run.  The
            # submission must re-read the quote immediately before validation.
            fresh_quote = self.broker.quote(intent["symbol"], risk)
            local_orders = [row for row in self.store.unresolved_orders() if row["cid"] != cid]
            # The service revalidates against the fresh account, so a price or
            # position change between the plan and the POST cannot slip through.
            validate_new_intent(
                intent,
                account=fresh,
                quote=fresh_quote,
                risk=risk,
                execution=self.execution,
                symbols=symbols,
                session=session,
                buy_attempts=buy_attempts,
                local_orders=local_orders,
                freeze_reason=freeze_reason,
                stop_allowed=self.execution.allow_broker_stop and self.execution.broker_stop_verified,
                paused=self.store.paused(),
            )
            if self.store.paused() or self.store.safety_reason():
                raise PreDispatchRejected("Execution paused or safety-locked before dispatch")
            if self.store.config_pending():
                raise PreDispatchRejected("Configuration pending restart")
            if not self.store.transition(
                cid, OrderState.READY_TO_DISPATCH,
                expected=OrderState.VALIDATING, owner=owner, reason="preflight_passed",
            ):
                raise PreDispatchRejected("Could not claim READY_TO_DISPATCH")
            # The final cutoff check must happen after all slow preflight work
            # and before the durable dispatch boundary.
            from .market_data import session_info

            if not self.execution:
                raise PreDispatchRejected("Execution contract missing")
            final_session = session_info(self.clock(), self.execution)
            if not final_session.can_submit:
                raise PreDispatchRejected("Outside submission cutoff at dispatch boundary")
            if self.store.record_dispatch_intent(cid, owner):
                pass
            else:
                raise PreDispatchRejected("Could not record dispatch intent")
            try:
                response = self.broker.dispatch(intent, cid)
            except Exception as exc:  # noqa: BLE001 - post-dispatch ambiguity is explicit
                return self._post_dispatch_failure(cid, owner, intent, exc)
            if not response:
                self.store.transition(
                    cid, OrderState.SUBMISSION_OUTCOME_UNKNOWN,
                    expected=OrderState.DISPATCH_INTENT_RECORDED, owner=owner,
                    reason="broker returned no order payload",
                )
                self.store.block("ORDER_UNKNOWN: no response from broker: " + cid)
                return {"cid": cid, "state": OrderState.SUBMISSION_OUTCOME_UNKNOWN.value, "unknown": True}
            state = self.store.apply_broker_order(cid, response)
            return {"cid": cid, "state": state.value if state else None, "order": response}
        except BrokerStopUnsupported as exc:
            row = self.store.order_row(cid) or {}
            if row.get("state") == OrderState.DISPATCH_INTENT_RECORDED.value:
                return self._post_dispatch_failure(cid, owner, intent, exc)
            self.store.mark_not_sent(cid, owner, "BROKER_STOP_UNSUPPORTED: " + str(exc))
            self.store.event("order_not_sent", {"cid": cid, "plan_id": plan_id, "trigger_id": trigger_id,
                                                 "reason": "BROKER_STOP_UNSUPPORTED", "detail": str(exc)})
            return {"cid": cid, "state": OrderState.NOT_SENT.value, "rejected": "BROKER_STOP_UNSUPPORTED"}
        except (RiskRejected, PreDispatchRejected, OrderValidationError) as exc:
            row = self.store.order_row(cid) or {}
            if row.get("state") == OrderState.DISPATCH_INTENT_RECORDED.value:
                # An exception type raised after the durable boundary is not
                # proof of "not sent"; reconcile it like any POST ambiguity.
                return self._post_dispatch_failure(cid, owner, intent, exc)
            reason = getattr(exc, "code", type(exc).__name__)
            self.store.mark_not_sent(cid, owner, f"{reason}: {exc}")
            self.store.event("order_not_sent", {"cid": cid, "plan_id": plan_id, "trigger_id": trigger_id,
                                                 "reason": reason, "detail": str(exc)})
            return {"cid": cid, "state": OrderState.NOT_SENT.value, "rejected": reason}
        except Exception as exc:  # noqa: BLE001 - unknown pre-dispatch failure
            row = self.store.order_row(cid) or {}
            if row.get("state") == OrderState.DISPATCH_INTENT_RECORDED.value:
                return self._post_dispatch_failure(cid, owner, intent, exc)
            self.store.mark_not_sent(cid, owner, "PRE_DISPATCH_ERROR: " + type(exc).__name__ + ": " + str(exc))
            self.store.error("order_submit", exc, cid=cid, cycle=plan_id, outcome="not_sent")
            return {"cid": cid, "state": OrderState.NOT_SENT.value, "rejected": "PRE_DISPATCH_ERROR"}

    def _post_dispatch_failure(self, cid: str, owner: str, intent: dict, exc: Exception) -> dict:
        """The POST may have reached Alpaca.  Reconcile; never mark NOT_SENT."""
        self.store.error(
            "order_submit", exc, cid=cid, ticker=intent.get("symbol"),
            cycle=intent.get("plan_id"), outcome="submission_outcome_unknown",
        )
        self.store.transition(
            cid, OrderState.SUBMISSION_OUTCOME_UNKNOWN,
            expected=OrderState.DISPATCH_INTENT_RECORDED, owner=owner,
            reason="post-dispatch exception: " + type(exc).__name__,
        )
        try:
            found = self.broker.lookup(cid)
        except Exception as lookup_exc:  # noqa: BLE001
            self.store.error("order_reconciliation", lookup_exc, cid=cid, ticker=intent.get("symbol"),
                             outcome="unknown_requires_reconciliation")
            self.store.block("ORDER_UNKNOWN: submission result could not be queried: " + cid)
            return {"cid": cid, "state": OrderState.SUBMISSION_OUTCOME_UNKNOWN.value, "unknown": True}
        if found:
            state = self.store.apply_broker_order(cid, found)
            return {"cid": cid, "state": state.value if state else None, "order": found}
        self.store.block(
            "ORDER_UNKNOWN: no broker record yet for a possibly-sent order: " + cid
        )
        return {"cid": cid, "state": OrderState.SUBMISSION_OUTCOME_UNKNOWN.value, "unknown": True}

    # ------------------------------------------------------------- recovery
    def reconcile(self, *, cancel_lingering: bool = False) -> dict:
        """Read-only broker reconciliation.

        ``cancel_lingering`` exists only for an explicitly authorized operator
        action.  The watchdog and the session never cancel DAY orders or stops.
        """
        store, broker = self.store, self.broker
        results = {"checked": 0, "updated": 0, "unknown": 0, "conflicts": 0, "cancelled": 0}
        for row in store.orders_for_reconcile():
            results["checked"] += 1
            cid = row["cid"]
            try:
                found = broker.lookup(cid)
            except Exception as exc:  # noqa: BLE001
                store.error("order_reconciliation", exc, cid=cid, outcome="reconciliation_retry")
                continue
            if found:
                before = row["state"]
                state = store.apply_broker_order(cid, found)
                if state and state.value != before:
                    results["updated"] += 1
                if state is OrderState.RECONCILIATION_REQUIRED:
                    results["conflicts"] += 1
                if cancel_lingering and state not in {OrderState.FILLED, OrderState.CANCELED,
                                                      OrderState.EXPIRED, OrderState.REJECTED,
                                                      OrderState.NOT_SENT}:
                    try:
                        broker.cancel(cid)
                        results["cancelled"] += 1
                    except Exception as exc:  # noqa: BLE001
                        store.error("order_cancel", exc, cid=cid, outcome="cancel_not_confirmed")
                continue
            # No broker order was found for this local row.
            current = OrderState(row["state"])
            if current in {OrderState.ACCEPTED, OrderState.PARTIALLY_FILLED}:
                store.transition(
                    cid, OrderState.RECONCILIATION_REQUIRED,
                    expected=current, reason="broker no longer reports a locally active order",
                )
                store.block("Local/broker order mismatch; reconciliation required: " + cid)
                results["conflicts"] += 1
            else:
                # A dispatch-intent or unknown order needs a grace period: the
                # POST may be in flight and the broker may not expose it yet.
                from .order_state import OrderState as _State
                from datetime import datetime, timezone

                sent_at = row.get("dispatch_intent_at")
                if sent_at:
                    try:
                        age = (self.clock() - datetime.fromisoformat(
                            str(sent_at).replace("Z", "+00:00")
                        )).total_seconds()
                    except ValueError:
                        age = None
                    if age is not None and age < getattr(
                        self.execution, "submission_grace_seconds", 30
                    ):
                        continue
                # Dispatch-intent/unknown rows are never converted to NOT_SENT
                # by a 404.  They stay unknown and block only new risk.
                store.transition(
                    cid, OrderState.SUBMISSION_OUTCOME_UNKNOWN,
                    expected=(OrderState.DISPATCH_INTENT_RECORDED, OrderState.SUBMISSION_OUTCOME_UNKNOWN),
                    reason="no broker record yet; may have been sent",
                )
                store.block("ORDER_UNKNOWN: no broker record yet for possibly-sent order: " + cid)
                results["unknown"] += 1
        # Import bot orders that exist at the broker but not locally.
        try:
            account = broker.snapshot()
        except Exception as exc:  # noqa: BLE001
            store.error("order_reconciliation", exc, outcome="snapshot_failed")
            return results
        for order in account.get("open_orders", []):
            cid = str(order.get("client_order_id") or "")
            if cid.startswith("aip-") and not store.order_row(cid):
                store.import_broker_order(cid, order)
                results["conflicts"] += 1
        residuals = {
            symbol: qty - int(qty)
            for symbol, qty in account.get("quantities", {}).items()
            if qty != int(qty)
        }
        if residuals:
            store.event("fractional_residual", {
                "quantities": residuals, "outcome": "manual_handling_only",
            })
        return results


def monitor(store, broker, cancel=False):
    """Compatibility entry point used by the independent watchdog."""
    return OrderService(store, broker, None).reconcile(cancel_lingering=cancel)
