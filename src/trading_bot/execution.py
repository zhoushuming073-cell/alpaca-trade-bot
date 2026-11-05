"""Durable, at-most-once submission and conservative REST reconciliation."""
import hashlib
from .safety import validate_order, IOCUnavailable
from .storage import TERMINAL


def monitor(store, broker, cancel=False):
    """No account snapshot here. Continue even when paused or outside the window."""
    for cid in store.pending():
        context = store.request(cid) or {}
        ticker=context.get("request",{}).get("symbol")
        try:
            found = broker.lookup(cid)
            if found:
                store.order(cid, found)
            if found and found.get("status") in TERMINAL:
                continue
            if cancel and found:
                try:
                    broker.cancel(cid)
                    store.event("cancel_requested", {"cid": cid, "cycle": context.get("cycle"), "outcome": "confirmation_pending"})
                except Exception as exc:
                    store.error("order_cancel", exc, cid=cid, ticker=ticker, cycle=context.get("cycle"), outcome="reconciliation_required")
                # DELETE success only acknowledges a request, never proves canceled.
                found = broker.lookup(cid)
                if found:
                    store.order(cid, found)
            if not found or (cancel and cid in store.pending()):
                store.uncertain(cid, "Order acceptance/fill/cancellation remains unconfirmed: " + cid)
                store.error("order_unknown", RuntimeError("Broker state not terminal or order not found"),
                            cid=cid, ticker=ticker, cycle=context.get("cycle"), outcome="reconciliation_required")
        except Exception as exc:
            store.uncertain(cid, "Order reconciliation network failure: " + cid)
            store.error("order_reconciliation", exc, cid=cid, ticker=ticker, cycle=context.get("cycle"),
                        retried=bool(getattr(exc, "_bot_retries", 0)), outcome="reconciliation_required")


def reconcile(store, broker):
    monitor(store, broker)
    account = broker.snapshot()
    store.event("account", account)
    residuals = {s: q - int(q) for s, q in account["quantities"].items() if q != int(q)}
    if residuals:
        store.event("fractional_residual", {"quantities": residuals, "outcome": "manual_handling_only"})
    # Recover bot orders found at the broker but missing locally (e.g. restored DB).
    for order in account["open_orders"]:
        cid = order.get("client_order_id", "")
        if cid.startswith("aip-") and cid not in store.pending():
            if store.reserve(cid, str(order.get("created_at", ""))[:10], {"recovered": True}):
                store.order(cid, order)
                store.block("Recovered orphan bot order; reconciliation required: " + cid)
            else:
                # Terminal locally but still active remotely: do not pretend terminal.
                store.db.execute("UPDATE orders SET status='reconciliation_required' WHERE cid=?", (cid,))
                store.db.commit()
                store.block("Local/broker order mismatch: " + cid)
    return account


def execute(store, broker, cycle, day, intent):
    validate_order(intent)
    cid = "aip-" + hashlib.sha256(f"{cycle}:{intent['symbol']}".encode()).hexdigest()[:36]
    # Existing reservations are never posted again, including after a crash.
    if store.request(cid):
        monitor(store, broker)
        return cid
    if store.paused() or store.safety_reason() or store.pending():
        raise ValueError("Paused, safety-locked or unresolved order")
    if not store.reserve(cid, day, intent, cycle):
        return cid
    store.event("order_request", {"cid": cid, "cycle": cycle, **intent})
    try:
        response = broker.submit(intent, cid)
        if response:
            store.order(cid, response)
    except Exception as exc:
        store.error("order_submit", exc, cid=cid, ticker=intent["symbol"], cycle=cycle, outcome="lookup_before_any_action")
        # A definitive broker HTTP rejection differs from a transport timeout.
        status = getattr(exc, "status_code", None)
        if isinstance(exc, IOCUnavailable) or status in {400, 401, 403, 422}:
            store.order(cid, {"status": "rejected", "reason": str(exc)})
            store.block("IOC submission rejected/unavailable; no fallback permitted")
            return cid
    monitor(store, broker)
    return cid
