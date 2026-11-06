"""Exam-only JSON normalization for execution metadata.

The production schemas intentionally accept only ``{"stop_price": ...}`` in
``stop_loss``.  Some otherwise-valid broker-style model answers include the
execution metadata ``type`` and ``time_in_force``.  The exam adapter removes
only those known presentation fields; it never changes the stop price, action,
quantity, limit, direction, or policy.
"""

from __future__ import annotations

import json


def normalize_stop_metadata(raw: str) -> tuple[str, bool]:
    try:
        data = json.loads(raw)
    except Exception:
        return raw, False
    changed = False

    def walk(value):
        nonlocal changed
        if isinstance(value, dict):
            stop = value.get("stop_loss")
            if isinstance(stop, (int, float, str)):
                value["stop_loss"] = {"stop_price": stop}
                changed = True
            elif isinstance(stop, dict):
                cleaned = dict(stop)
                if "price" in cleaned and "stop_price" not in cleaned:
                    cleaned["stop_price"] = cleaned.pop("price")
                    changed = True
                for key in ("type", "time_in_force", "order_type"):
                    if key in cleaned:
                        cleaned.pop(key)
                        changed = True
                value["stop_loss"] = cleaned
            for item in list(value.values()):
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(data)
    return (json.dumps(data, ensure_ascii=False), True) if changed else (raw, False)


def normalize_plan(raw: str) -> tuple[str, bool]:
    """Make non-executable model candidates schema-consistent.

    The decision itself is preserved.  A HOLD/WAIT/NO_TRADE candidate cannot
    carry an executable order; removing that contradiction prevents an invalid
    plan from being silently treated as tradable.
    """
    try:
        data = json.loads(raw)
    except Exception:
        return raw, False
    changed = False
    market = str(data.get("market_decision") or "").upper()
    non_trading = {"HOLD", "WAIT", "NO_TRADE"}
    trading = {"OPEN_LONG", "CLOSE_LONG", "OPEN_SHORT", "COVER_SHORT", "BUY", "SELL", "REDUCE"}
    candidates = []
    for candle in data.get("candidates") or []:
        decision = str(candle.get("trade_decision") or "").upper()
        if market != "TRADE" or decision in non_trading or candle.get("veto"):
            if candle.get("order_intent") is not None:
                candle["order_intent"] = None
                changed = True
            triggers = [t for t in candle.get("triggers") or []
                        if str(t.get("purpose") or "").upper() != "EXECUTE_ORDER"]
            if triggers != (candle.get("triggers") or []):
                candle["triggers"] = triggers
                changed = True
        if decision in trading and candle.get("order_intent") is None:
            # A trading label without an executable order is not tradable.
            changed = True
            continue
        candidates.append(candle)
    if candidates != (data.get("candidates") or []):
        data["candidates"] = candidates
    return (json.dumps(data, ensure_ascii=False), True) if changed else (raw, False)

