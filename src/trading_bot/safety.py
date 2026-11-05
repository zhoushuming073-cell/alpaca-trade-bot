"""Order invariants and credential-safe audit helpers.

The trading contract is intentionally narrow:

* Alpaca Paper only, US equities/ETFs, long only.
* Whole-share DAY LIMIT orders, regular hours only.
* No notional orders, no leverage, no shorting.
* An optional broker-side stop may be requested by the AI; it is only valid
  when the execution capability has been explicitly verified.  A requested
  stop is never silently dropped.

``validate_order`` accepts both an intent dictionary and the final SDK wire
dictionary.  It never changes a quantity; callers must reject an invalid
intent instead of shrinking it.
"""

from __future__ import annotations

import os
import re
from decimal import Decimal, InvalidOperation

from .settings import Strict  # noqa: F401  (re-export for older callers)


class OrderValidationError(ValueError):
    pass


class IOCUnavailable(RuntimeError):
    """Backward-compatible name for an unsupported broker execution mode."""


class BrokerStopUnsupported(RuntimeError):
    """The AI requested a broker stop that this account/path cannot provide."""


class PreDispatchRejected(RuntimeError):
    """A deterministic local/broker check failed before any POST was attempted."""


def redact(value):
    if isinstance(value, dict):
        return {
            str(k): (
                "[REDACTED]"
                if re.search(r"key|secret|token|authorization|password|credential", str(k), re.I)
                else redact(v)
            )
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        for k, secret in os.environ.items():
            if re.search(r"KEY|SECRET|TOKEN|PASSWORD", k) and len(secret) >= 4:
                value = value.replace(secret, "[REDACTED]")
        value = re.sub(r"(?i)(Bearer\s+)\S+", r"\1[REDACTED]", value)
        value = re.sub(
            r"""(?ix)((?:[\w-]*(?:api[_-]?key|secret|authorization|token|password)[\w-]*)["']?\s*[:=]\s*)[^\r\n,}]+""",
            r"\1[REDACTED]",
            value,
        )
        return value
    return value


def _decimal(value, label: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise OrderValidationError(f"{label} required")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise OrderValidationError(f"Invalid {label}") from exc
    if not number.is_finite():
        raise OrderValidationError(f"Invalid {label}")
    return number


def _side(order: dict) -> str:
    raw = order.get("side") or order.get("action") or ""
    if hasattr(raw, "value"):
        raw = raw.value
    side = str(raw).lower()
    if side not in {"buy", "sell"}:
        raise OrderValidationError("US equity symbol and long-only side required")
    return side


def _stop_price(order: dict) -> Decimal | None:
    stop = order.get("stop_loss")
    if stop is None:
        stop = order.get("stop_price")
    if stop is None:
        return None
    if isinstance(stop, dict):
        stop = stop.get("stop_price")
    elif hasattr(stop, "stop_price"):
        stop = stop.stop_price
    return _decimal(stop, "stop price")


def _order_class(order: dict) -> str:
    value = order.get("order_class", "simple")
    if value is None or value == "":
        return "simple"
    return str(getattr(value, "value", value)).lower()


def validate_order(order: dict, *, allow_stop: bool = False) -> int:
    """Validate an order intent or final SDK wire payload.

    Returns the whole-share quantity.  Raises instead of adjusting anything.
    """
    if not isinstance(order, dict):
        raise OrderValidationError("Order must be a mapping")
    if "notional" in order:
        raise OrderValidationError("Notional orders are forbidden")
    qty = _decimal(order.get("qty", order.get("quantity")), "whole-share qty")
    if qty <= 0 or qty != qty.to_integral_value():
        raise OrderValidationError("Positive whole-share qty required; fractions forbidden")
    price = _decimal(order.get("limit_price"), "limit price")
    if price <= 0:
        raise OrderValidationError("Positive limit price required")
    if price.as_tuple().exponent < -2:
        raise OrderValidationError("Limit price supports at most two decimals")
    symbol = str(order.get("symbol") or "").strip()
    if not symbol:
        raise OrderValidationError("Symbol required")
    side = _side(order)
    tif = str(getattr(order.get("time_in_force", "day"), "value", order.get("time_in_force", "day"))).lower()
    if tif != "day":
        raise OrderValidationError("Only DAY orders are permitted for new orders")
    otype = str(getattr(order.get("type", "limit"), "value", order.get("type", "limit"))).lower()
    if otype != "limit":
        raise OrderValidationError("Only LIMIT orders are permitted")
    if order.get("extended_hours", False):
        raise OrderValidationError("Extended-hours orders are forbidden")
    klass = _order_class(order)
    if klass in {"bracket", "oco"}:
        raise OrderValidationError("Bracket/OCO orders are forbidden")
    if klass not in {"simple", "oto"}:
        raise OrderValidationError("Only simple or OTO orders are permitted")
    stop = _stop_price(order)
    if klass == "oto" or stop is not None:
        if not allow_stop:
            raise OrderValidationError("Broker stop execution is not enabled")
        if stop is None:
            raise OrderValidationError("OTO orders require a stop price")
        if stop <= 0:
            raise OrderValidationError("Positive stop price required")
        if side == "buy" and stop >= price:
            raise OrderValidationError("BUY stop must be below the limit price")
        if side == "sell" and stop <= price:
            raise OrderValidationError("SELL stop must be above the limit price")
    if any(
        order.get(key) is not None
        for key in ("take_profit", "trail_price", "trail_percent", "legs")
    ):
        raise OrderValidationError("Conditional or multileg orders forbidden")
    if order.get("time_in_force", "day") is None:
        raise OrderValidationError("time_in_force required")
    return int(qty)


def validate_intent(intent: dict, *, allow_stop: bool = False) -> int:
    """Validate the AI order intent shape (used by the risk layer)."""
    required = ("symbol", "side", "qty", "limit_price", "order_type", "time_in_force")
    missing = [key for key in required if key not in intent]
    if missing:
        raise OrderValidationError("Missing order intent fields: " + ", ".join(missing))
    if str(intent.get("order_type")).lower() != "limit":
        raise OrderValidationError("Only LIMIT orders are permitted")
    if str(intent.get("time_in_force")).lower() != "day":
        raise OrderValidationError("Only DAY orders are permitted")
    if intent.get("extended_hours", False):
        raise OrderValidationError("Extended-hours orders are forbidden")
    return validate_order(
        {
            **intent,
            "type": "limit",
            "order_class": "oto" if intent.get("stop_policy") == "BROKER_STOP" else "simple",
        },
        allow_stop=allow_stop,
    )
