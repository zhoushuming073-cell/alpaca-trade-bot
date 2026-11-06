"""Deterministic execution risk checks.

The AI owns the trade decision, direction and exact share quantity.  Python
never resizes an order or converts a rejected short/long into another
direction.  It only verifies the hard contract:

* Paper-only, fixed watchlist, whole shares, DAY LIMIT, regular hours.
* Long opens use real non-margin cash.  Short opens require the explicit
  ``allow_shorting`` capability, Alpaca ``shorting_enabled`` and broker-side
  asset/borrow checks; buying_power is the margin check for shorts.
* Sell_to_close and buy_to_close are distinct from sell_to_open and
  buy_to_open.  One order may never cross zero; close first, then open.
* Unknown/conflicting account or order facts freeze **new risk**.
* Retained non-positional circuit breakers: daily loss and daily order
  attempts.  They block new opening risk but never block a legitimate close.
* A requested broker stop is rejected as a whole intent when the capability is
  not verified; it is never downgraded to NONE.
"""

from __future__ import annotations

import math
from decimal import Decimal, ROUND_FLOOR

from .order_state import TERMINAL_STATES, OrderState
from .safety import BrokerStopUnsupported, OrderValidationError, validate_intent

EPS = 1e-9


class RiskRejected(ValueError):
    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


def _payload_intent(row: dict) -> dict:
    import json

    raw = row.get("intent") or (row.get("payload") or "{}")
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return {}
    if isinstance(value, dict) and isinstance(value.get("intent"), dict):
        value = value["intent"]
    return value if isinstance(value, dict) else {}


def _remaining(order: dict) -> float:
    try:
        qty = Decimal(str(order.get("qty") or 0))
        filled = Decimal(str(order.get("filled_qty") or 0))
    except Exception:
        return 0.0
    return float(max(Decimal(0), qty - filled))


def _is_stop(order: dict) -> bool:
    kind = str(getattr(order.get("type"), "value", order.get("type")) or "").lower()
    if kind in {"stop", "stop_limit"}:
        return True
    for leg in order.get("legs") or []:
        leg_kind = str(leg.get("type") or leg.get("order_type") or "").lower()
        if leg_kind in {"stop", "stop_limit"}:
            return True
    return False


def reserved_buy_cash(account: dict, local_orders: list[dict] | None = None) -> float:
    total = 0.0
    open_orders = account.get("open_orders", []) or []
    broker_cids = {str(order.get("client_order_id") or "") for order in open_orders}
    for order in open_orders:
        side = str(order.get("side") or "").lower()
        if side != "buy":
            continue
        try:
            price = float(order.get("limit_price") or 0)
        except (TypeError, ValueError):
            continue
        total += max(0.0, _remaining(order)) * max(0.0, price)
    for row in local_orders or []:
        try:
            state = OrderState(row.get("state"))
        except ValueError:
            continue
        if state in TERMINAL_STATES:
            continue
        # Once an order is visible at the broker, the broker snapshot is the
        # authoritative reservation; do not count the same cid twice.
        if str(row.get("cid") or "") in broker_cids:
            continue
        intent = _payload_intent(row)
        if str(intent.get("side") or intent.get("action") or "").lower() != "buy":
            continue
        total += max(0.0, float(intent.get("qty") or 0)) * max(0.0, float(intent.get("limit_price") or 0))
    return total


def reserved_sell_qty(
    symbol: str, account: dict, local_orders: list[dict] | None = None
) -> float:
    total = 0.0
    open_orders = account.get("open_orders", []) or []
    broker_cids = {str(order.get("client_order_id") or "") for order in open_orders}
    for order in open_orders:
        if str(order.get("symbol") or "") != symbol:
            continue
        if str(order.get("side") or "").lower() == "sell":
            total += max(0.0, _remaining(order))
        # An OTO parent BUY can carry a sell stop leg; those shares are
        # protected and cannot be sold again by a later ordinary sell.
        for leg in order.get("legs") or []:
            leg_side = str(leg.get("side") or "").lower()
            leg_type = str(leg.get("type") or leg.get("order_type") or "").lower()
            if leg_side == "sell" or (leg_type in {"stop", "stop_limit"} and str(order.get("side") or "").lower() == "buy"):
                total += max(0.0, _remaining(leg))
    for row in local_orders or []:
        try:
            state = OrderState(row.get("state"))
        except ValueError:
            continue
        if state in TERMINAL_STATES:
            continue
        if str(row.get("cid") or "") in broker_cids:
            continue
        intent = _payload_intent(row)
        if str(intent.get("symbol") or "") != symbol:
            continue
        if str(intent.get("side") or intent.get("action") or "").lower() != "sell":
            continue
        total += max(0.0, float(intent.get("qty") or 0))
    return total


def stop_orders(account: dict, symbol: str | None = None) -> list[dict]:
    result = []
    for order in account.get("open_orders", []) or []:
        if symbol and str(order.get("symbol") or "") != symbol:
            continue
        if _is_stop(order):
            result.append(order)
    return result


def sellable_qty(account: dict, symbol: str, local_orders: list[dict] | None = None) -> int:
    held = float(account.get("quantities", {}).get(symbol, 0) or 0)
    if not math.isfinite(held):
        raise RiskRejected("INVALID_POSITION", "invalid position")
    if held <= 0:
        return 0
    held_whole = int(Decimal(str(held)).to_integral_value(rounding=ROUND_FLOOR))
    available = float(account.get("available_quantities", {}).get(symbol, held))
    reserved = reserved_sell_qty(symbol, account, local_orders)
    available_whole = int(
        Decimal(str(min(held - reserved, available))).to_integral_value(rounding=ROUND_FLOOR)
    )
    return max(0, min(held_whole - max(0, int(reserved)), available_whole))


def coverable_qty(account: dict, symbol: str, local_orders: list[dict] | None = None) -> int:
    """Whole shares available to buy-to-close an existing short position."""
    held = float(account.get("quantities", {}).get(symbol, 0) or 0)
    if not math.isfinite(held):
        raise RiskRejected("INVALID_POSITION", "invalid position")
    short_qty = max(0.0, -held)
    short_whole = int(Decimal(str(short_qty)).to_integral_value(rounding=ROUND_FLOOR))
    reserved = reserved_cover_qty(symbol, account, local_orders)
    return max(0, short_whole - max(0, int(reserved)))


def reserved_cover_qty(symbol: str, account: dict, local_orders: list[dict] | None = None) -> float:
    total = 0.0
    open_orders = account.get("open_orders", []) or []
    broker_cids = {str(order.get("client_order_id") or "") for order in open_orders}
    for order in open_orders:
        if str(order.get("symbol") or "") != symbol:
            continue
        intent = str(getattr(order.get("position_intent"), "value", order.get("position_intent")) or "").lower()
        if str(order.get("side") or "").lower() == "buy" and intent == "buy_to_close":
            total += max(0.0, _remaining(order))
    for row in local_orders or []:
        try:
            state = OrderState(row.get("state"))
        except ValueError:
            continue
        if state in TERMINAL_STATES or str(row.get("cid") or "") in broker_cids:
            continue
        intent = _payload_intent(row)
        if str(intent.get("symbol") or "") != symbol:
            continue
        if str(intent.get("position_intent") or "").lower() == "buy_to_close":
            total += max(0.0, float(intent.get("qty") or 0))
    return total


def intent_position_intent(intent: dict) -> str:
    raw = intent.get("position_intent")
    if raw:
        value = str(getattr(raw, "value", raw)).lower()
        if value in {"buy_to_open", "sell_to_close", "sell_to_open", "buy_to_close"}:
            return value
        raise RiskRejected("INVALID_POSITION_INTENT", value)
    action = str(intent.get("action") or "").upper()
    side = str(intent.get("side") or "").lower()
    mapping = {
        "BUY": "buy_to_open", "OPEN_LONG": "buy_to_open",
        "SELL": "sell_to_close", "REDUCE": "sell_to_close",
        "CLOSE_LONG": "sell_to_close", "OPEN_SHORT": "sell_to_open",
        "COVER_SHORT": "buy_to_close",
    }
    if action in mapping:
        return mapping[action]
    return "buy_to_open" if side == "buy" else "sell_to_close"


def validate_new_intent(
    intent: dict,
    *,
    account: dict,
    quote: dict,
    risk,
    execution,
    symbols,
    session,
    buy_attempts: int,
    local_orders: list[dict] | None = None,
    freeze_reason: str = "",
    stop_allowed: bool = False,
    paused: bool = False,
) -> int:
    """Validate one AI order intent.  Returns the unchanged quantity.

    ``session`` must expose ``active`` and ``can_submit``/``now``; the engine
    supplies the authoritative exchange-calendar view.
    """
    symbol = str(intent.get("symbol") or "")
    side = str(intent.get("side") or intent.get("action") or "").lower()
    if symbol not in symbols:
        raise RiskRejected("OUTSIDE_WATCHLIST", symbol)
    if side not in {"buy", "sell"}:
        raise RiskRejected("INVALID_SIDE")
    position_intent = intent_position_intent(intent)
    if (side == "buy") != (position_intent in {"buy_to_open", "buy_to_close"}):
        raise RiskRejected("POSITION_INTENT_SIDE_MISMATCH", position_intent)
    if freeze_reason:
        # An unknown/conflicting order fact can make even a sell unsafe.
        raise RiskRejected("FROZEN", freeze_reason)
    if paused:
        raise RiskRejected("PAUSED")
    if not session.active:
        raise RiskRejected("OUTSIDE_ACTIVE_WINDOW", str(getattr(session, "phase", "")))
    if not session.can_submit:
        raise RiskRejected("AFTER_SUBMIT_CUTOFF", str(session.now_et))
    if account.get("blocked"):
        raise RiskRejected("ACCOUNT_BLOCKED")
    equity = float(account.get("equity") or 0)
    last_equity = float(account.get("last_equity") or 0)
    cash = float(account.get("cash") or 0)
    if not all(math.isfinite(v) for v in (equity, last_equity, cash)) or equity <= 0:
        raise RiskRejected("INVALID_ACCOUNT", "non-finite or non-positive equity")
    if last_equity <= 0:
        raise RiskRejected("INVALID_ACCOUNT", "last_equity missing")
    for name, value in list(account.get("positions", {}).items()):
        if not math.isfinite(float(value)):
            raise RiskRejected("INVALID_POSITION", name)
    for name, value in list(account.get("quantities", {}).items()):
        if not math.isfinite(float(value)):
            raise RiskRejected("INVALID_POSITION", name)
        if float(value) < 0 and not execution.allow_shorting:
            raise RiskRejected("INVALID_POSITION", "short position while shorting disabled: " + name)

    stop_policy = str(intent.get("stop_policy") or "NONE").upper()
    if stop_policy not in {"NONE", "BROKER_STOP"}:
        raise RiskRejected("INVALID_STOP_POLICY", stop_policy)
    if stop_policy == "BROKER_STOP" and not stop_allowed:
        # Reject the whole intent; never silently add/remove protection.
        raise BrokerStopUnsupported(
            "BROKER_STOP requested but not verified for this Alpaca Paper account"
        )
    if stop_policy == "BROKER_STOP" and position_intent not in {"buy_to_open", "sell_to_open"}:
        raise BrokerStopUnsupported("BROKER_STOP is only supported for opening intents")
    if stop_policy == "NONE" and intent.get("stop_loss") not in (None, {}):
        raise RiskRejected("UNEXPECTED_STOP", "NONE must not carry stop parameters")
    try:
        qty = validate_intent(intent, allow_stop=stop_allowed)
    except OrderValidationError as exc:
        raise RiskRejected("INVALID_ORDER", str(exc)) from exc
    except BrokerStopUnsupported:
        raise
    if int(intent.get("qty", qty)) != qty:
        raise RiskRejected("QTY_MISMATCH")

    # Obvious order conflicts are resolved before any new risk is added.
    for order in account.get("open_orders", []) or []:
        if str(order.get("symbol") or "") != symbol:
            continue
        open_side = str(order.get("side") or "").lower()
        if side == "buy" and (open_side == "sell" or _is_stop(order)):
            raise RiskRejected("CONFLICTING_ORDER", "existing sell/stop on symbol")
        if side == "sell" and open_side == "buy":
            raise RiskRejected("CONFLICTING_ORDER", "existing buy on symbol")

    price = Decimal(str(intent["limit_price"]))
    bid = Decimal(str(quote.get("bid") or 0))
    ask = Decimal(str(quote.get("ask") or 0))
    if not all(x.is_finite() for x in (bid, ask)) or not (0 < bid <= ask):
        raise RiskRejected("INVALID_QUOTE", "bid/ask missing or crossed")
    age = quote.get("quote_age_seconds")
    if age is None or not math.isfinite(float(age)) or not 0 <= float(age) <= risk.max_quote_age_seconds:
        raise RiskRejected("STALE_QUOTE", "quote age missing or above maximum")
    last_age = quote.get("last_age_seconds")
    if last_age is not None and (not math.isfinite(float(last_age)) or float(last_age) > risk.max_quote_age_seconds):
        raise RiskRejected("STALE_QUOTE", "last trade age above maximum")
    if float((ask - bid) / bid) > risk.max_spread:
        raise RiskRejected("QUOTE_SPREAD", "spread above configured maximum")
    reference = quote.get("last") or quote.get("close")
    if reference:
        reference = Decimal(str(reference))
        if reference > 0 and abs(float(ask / reference - 1)) > risk.max_price_deviation:
            raise RiskRejected("QUOTE_DEVIATION", "ask too far from last price")

    qty_signed = float(account.get("quantities", {}).get(symbol, 0) or 0)
    long_qty = max(0.0, qty_signed)
    short_qty = max(0.0, -qty_signed)
    if position_intent == "buy_to_open" and short_qty > 0:
        raise RiskRejected("WOULD_CROSS_ZERO", "cover the short with a separate COVER_SHORT order first")
    if position_intent == "sell_to_open" and long_qty > 0:
        raise RiskRejected("WOULD_CROSS_ZERO", "close the long with a separate CLOSE_LONG order first")
    if position_intent == "sell_to_close" and long_qty <= 0:
        raise RiskRejected("NO_LONG_POSITION", "sell_to_close requires an existing long")
    if position_intent == "buy_to_close" and short_qty <= 0:
        raise RiskRejected("NO_SHORT_POSITION", "buy_to_close requires an existing short")
    if position_intent == "sell_to_open" and not execution.allow_shorting:
        raise RiskRejected("SHORTING_DISABLED", "allow_shorting is false in execution.yaml")
    if position_intent == "sell_to_open" and not bool(account.get("shorting_enabled")):
        raise RiskRejected("SHORTING_NOT_ENABLED", "Alpaca account shorting_enabled is false")

    if position_intent in {"buy_to_open", "sell_to_open"}:
        if equity <= last_equity * (1 - risk.max_daily_loss):
            raise RiskRejected("DAILY_LOSS_LIMIT")
        if buy_attempts >= risk.max_daily_orders:
            raise RiskRejected("DAILY_ORDER_LIMIT")

    if position_intent == "buy_to_open":
        available = max(0.0, cash - reserved_buy_cash(account, local_orders))
        cost = qty * float(price)
        if cost > available + EPS:
            raise RiskRejected(
                "INSUFFICIENT_CASH",
                f"need {cost:.2f}, non-margin available {available:.2f}",
            )
    elif position_intent == "sell_to_open":
        buying_power = float(account.get("buying_power") or 0)
        if not math.isfinite(buying_power) or buying_power <= 0:
            raise RiskRejected("INVALID_BUYING_POWER")
        cost = qty * float(price)
        if cost > buying_power + EPS:
            raise RiskRejected(
                "INSUFFICIENT_BUYING_POWER",
                f"short notional {cost:.2f}, Alpaca buying_power {buying_power:.2f}",
            )
    elif position_intent == "sell_to_close":
        available = sellable_qty(account, symbol, local_orders)
        if qty > available:
            raise RiskRejected(
                "INSUFFICIENT_SHARES", f"need {qty}, unencumbered whole shares {available}"
            )
    elif position_intent == "buy_to_close":
        available = coverable_qty(account, symbol, local_orders)
        if qty > available:
            raise RiskRejected(
                "INSUFFICIENT_SHORT_SHARES", f"need {qty}, coverable whole shares {available}"
            )
    return qty


def legacy_plan(decision, account, quote, values, policy, risk, symbols, count, paused):
    """Compatibility shim for old imports; raises with a clear migration hint."""
    raise RuntimeError(
        "target_weight planning was removed; use validate_new_intent with AI qty"
    )
