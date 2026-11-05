"""Deterministic execution risk checks.

The AI owns the trade decision and the exact share quantity.  Python never
resizes an order.  It only verifies the hard contract:

* Paper-only, fixed watchlist, long-only, whole shares, DAY LIMIT, regular hours.
* No notional, no margin/leverage, no shorting.
* Real cash (``cash``, not margin-inflated ``buying_power``) minus reserved
  open BUY orders.
* Sell quantity cannot exceed unencumbered whole shares (existing sells and
  stop orders reserve shares).
* Unknown/conflicting account or order facts freeze **new risk**.
* Retained non-positional circuit breakers: daily loss and daily order
  attempts.  They block new BUY risk but never block a legitimate reduction.
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
    for order in account.get("open_orders", []) or []:
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
        intent = _payload_intent(row)
        if str(intent.get("side") or intent.get("action") or "").lower() != "buy":
            continue
        total += max(0.0, float(intent.get("qty") or 0)) * max(0.0, float(intent.get("limit_price") or 0))
    return total


def reserved_sell_qty(
    symbol: str, account: dict, local_orders: list[dict] | None = None
) -> float:
    total = 0.0
    for order in account.get("open_orders", []) or []:
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
    if held < 0 or not math.isfinite(held):
        raise RiskRejected("INVALID_POSITION", "negative or invalid position")
    held_whole = int(Decimal(str(held)).to_integral_value(rounding=ROUND_FLOOR))
    available = float(account.get("available_quantities", {}).get(symbol, held))
    reserved = reserved_sell_qty(symbol, account, local_orders)
    available_whole = int(
        Decimal(str(min(held - reserved, available))).to_integral_value(rounding=ROUND_FLOOR)
    )
    return max(0, min(held_whole - max(0, int(reserved)), available_whole))


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
        if not math.isfinite(float(value)) or float(value) < 0:
            raise RiskRejected("INVALID_POSITION", name)
    for name, value in list(account.get("quantities", {}).items()):
        if not math.isfinite(float(value)) or float(value) < 0:
            raise RiskRejected("INVALID_POSITION", name)

    stop_policy = str(intent.get("stop_policy") or "NONE").upper()
    if stop_policy not in {"NONE", "BROKER_STOP"}:
        raise RiskRejected("INVALID_STOP_POLICY", stop_policy)
    if stop_policy == "BROKER_STOP" and not stop_allowed:
        # Reject the whole intent; never silently add/remove protection.
        raise BrokerStopUnsupported(
            "BROKER_STOP requested but not verified for this Alpaca Paper account"
        )
    if stop_policy == "BROKER_STOP" and side != "buy":
        raise BrokerStopUnsupported("BROKER_STOP is only supported for BUY intents")
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

    if side == "buy":
        if equity <= last_equity * (1 - risk.max_daily_loss):
            raise RiskRejected("DAILY_LOSS_LIMIT")
        if buy_attempts >= risk.max_daily_orders:
            raise RiskRejected("DAILY_ORDER_LIMIT")
        available = max(0.0, cash - reserved_buy_cash(account, local_orders))
        cost = qty * float(price)
        if cost > available + EPS:
            raise RiskRejected(
                "INSUFFICIENT_CASH",
                f"need {cost:.2f}, non-margin available {available:.2f}",
            )
        # A buy must not be used to cover a short position.
        if float(account.get("quantities", {}).get(symbol, 0) or 0) < 0:
            raise RiskRejected("SHORT_POSITION")
    else:
        available = sellable_qty(account, symbol, local_orders)
        if qty > available:
            raise RiskRejected(
                "INSUFFICIENT_SHARES", f"need {qty}, unencumbered whole shares {available}"
            )
    return qty


def legacy_plan(decision, account, quote, values, policy, risk, symbols, count, paused):
    """Compatibility shim for old imports; raises with a clear migration hint."""
    raise RuntimeError(
        "target_weight planning was removed; use validate_new_intent with AI qty"
    )
