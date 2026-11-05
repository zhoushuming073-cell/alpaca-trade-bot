"""Shared order invariants and credential-safe audit helpers."""
import os
import re
from decimal import Decimal, InvalidOperation


def redact(value):
    if isinstance(value, dict):
        return {str(k): ("[REDACTED]" if re.search(r"key|secret|token|authorization|password|credential", str(k), re.I)
                         else redact(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        for k, secret in os.environ.items():
            if re.search(r"KEY|SECRET|TOKEN|PASSWORD", k) and len(secret) >= 4:
                value = value.replace(secret, "[REDACTED]")
        value = re.sub(r"(?i)(Bearer\s+)\S+", r"\1[REDACTED]", value)
        value = re.sub(r"""(?ix)((?:[\w-]*(?:api[_-]?key|secret|authorization|token|password)[\w-]*)["']?\s*[:=]\s*)[^\r\n,}]+""", r"\1[REDACTED]", value)
        return value
    return value


def validate_order(order):
    """Accept an intent or SDK/wire dict. Never convert a fractional quantity."""
    if "notional" in order:
        raise ValueError("Notional orders are forbidden")
    quantity = order.get("qty", order.get("quantity"))
    if isinstance(quantity, bool) or quantity is None:
        raise ValueError("Positive whole-share qty required")
    try:
        qty = Decimal(str(quantity))
        price = Decimal(str(order.get("limit_price")))
    except InvalidOperation as exc:
        raise ValueError("Invalid order number") from exc
    if not qty.is_finite() or qty <= 0 or qty != qty.to_integral_value():
        raise ValueError("Positive whole-share qty required; fractions forbidden")
    if not price.is_finite() or price <= 0:
        raise ValueError("Positive finite limit price required")
    if order.get("side") not in ("buy", "sell") or not order.get("symbol"):
        raise ValueError("US equity symbol and long-only side required")
    if order.get("time_in_force", "ioc") != "ioc" or order.get("type", "limit") != "limit":
        raise ValueError("Only IOC LIMIT orders are permitted; no DAY fallback")
    if order.get("extended_hours", False) or order.get("order_class", "simple") not in (None, "", "simple"):
        raise ValueError("Only simple regular-session orders permitted")
    if any(order.get(k) is not None for k in ("stop_price", "trail_price", "trail_percent", "legs", "take_profit", "stop_loss")):
        raise ValueError("Conditional or multileg orders forbidden")
    return int(qty)


class IOCUnavailable(RuntimeError):
    pass
