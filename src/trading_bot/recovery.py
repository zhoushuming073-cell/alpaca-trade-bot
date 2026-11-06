"""Explicit operator recovery: GET-only broker facts, durable local audit, no resume."""
import json
from collections import defaultdict
from decimal import Decimal


def collect_pages(get, path, parameters, *, size, cursor_name):
    rows, seen = [], set()
    params = dict(parameters)
    for _ in range(100):
        page = get(path, params)
        if not isinstance(page, list):
            raise ValueError("Broker returned non-list history")
        for item in page:
            if not item.get("id") or item["id"] in seen:
                raise ValueError("Missing/repeated broker history ID")
            seen.add(item["id"])
        rows.extend(page)
        if len(page) < size:
            return rows
        params[cursor_name] = page[-1]["id"]
    raise ValueError("History pagination exceeded safety bound")


def validate_facts(orders, activities, account):
    """Full account-history recovery only. Missing fills or adjustments block approval."""
    if account.get("blocked"):
        raise ValueError("Broker account is blocked")
    if account.get("open_orders"):
        raise ValueError("Open broker orders require individual operator review")
    positions, filled = defaultdict(Decimal), defaultdict(Decimal)
    cash = Decimal(0)
    order_ids = {o["id"] for o in orders}
    for event in activities:
        if event.get("activity_type") == "FILL":
            if event.get("side") not in {"buy", "sell"} or event.get("order_id") not in order_ids:
                raise ValueError("Unmatched or unsupported fill")
            qty, price = Decimal(event["qty"]), Decimal(event["price"])
            if not qty.is_finite() or not price.is_finite() or qty <= 0 or price <= 0:
                raise ValueError("Invalid broker fill")
            sign = 1 if event["side"] == "buy" else -1
            positions[event["symbol"]] += sign * qty
            filled[event["order_id"]] += qty
            cash -= sign * qty * price
        elif event.get("activity_type") in {"JNLC", "FEE", "DIV", "INT", "CSD", "CSW"}:
            amount = Decimal(event["net_amount"])
            if not amount.is_finite():
                raise ValueError("Invalid cash activity")
            cash += amount
        else:
            raise ValueError("Non-trade activity requires manual reconciliation: " + str(event.get("activity_type")))
    for order in orders:
        if order.get("status") not in {"filled", "canceled", "expired", "rejected"}:
            raise ValueError("Order not confirmed terminal")
        if filled[order["id"]] != Decimal(str(order.get("filled_qty") or 0)):
            raise ValueError("Order/fill quantity mismatch")
    actual = {s: Decimal(str(q)) for s, q in account["quantities"].items() if Decimal(str(q)) != 0}
    expected = {s: q for s, q in positions.items() if q != 0}
    if expected != actual:
        raise ValueError("Fill ledger does not reproduce broker positions")
    if any(q < 0 or q != q.to_integral_value() for q in actual.values()):
        raise ValueError("Short or fractional holdings require manual review")
    if abs(cash - Decimal(str(account["cash"]))) > Decimal("0.01"):
        raise ValueError("Cash ledger does not reproduce broker cash")
    return {"orders": len(orders), "fills": sum(a.get("activity_type") == "FILL" for a in activities),
            "activities": len(activities), "positions": {s: str(q) for s, q in actual.items()},
            "cash": str(cash), "open_orders": 0}


def rebuild(store, orders, activities, account):
    if not store.paused():
        raise ValueError("Pause trading before recovery")
    result = validate_facts(orders, activities, account)
    remote = {o.get("client_order_id") for o in orders}
    if any(row["cid"] not in remote for row in store.unresolved_orders()):
        raise ValueError("Local unresolved order absent from broker history")
    # Individual fills retain actual execution timestamps and broker IDs. Existing
    # fills table keeps cumulative order fills for compatibility with the engine.
    with store._tx():
        store.db.execute("CREATE TABLE IF NOT EXISTS broker_activities(id TEXT PRIMARY KEY, ts TEXT, kind TEXT, payload TEXT)")
        for a in activities:
            store.db.execute("INSERT OR IGNORE INTO broker_activities VALUES(?,?,?,?)",
                             (a["id"], a.get("transaction_time") or a.get("date"), a["activity_type"], json.dumps(a)))
    for order in orders:
        cid = order.get("client_order_id")
        if not cid:
            raise ValueError("Broker order missing client identifier")
        if not store.order_row(cid):
            store.import_broker_order(cid, order)
        store.apply_broker_order(cid, order)
        row = store.order_row(cid)
        if row["state"] != str(order["status"]).upper():
            raise ValueError("Order state conflict remains after broker import")
    if store.has_unknown_orders():
        raise ValueError("Unknown orders remain after import")
    store.event("account", account)
    store.event("database_reconciliation", {**result, "outcome": "matched", "source": "Alpaca GET history",
                                           "missing_ai_history_recovered": False})
    # Recovery only: operator must explicitly clear this after backup/worker checks.
    store.set_control("autopilot", "false")
    store.block("DATABASE_RESTORED_RECONCILIATION_REQUIRED")
    return result
