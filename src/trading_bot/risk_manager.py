import math
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
from .safety import validate_order
from .storage import TERMINAL


def reserved_sells(account, symbol):
    reserved = Decimal(0)
    for order in account["open_orders"]:
        if order.get("status") in TERMINAL:
            continue
        if not order.get("symbol") or order.get("side") not in ("buy", "sell"):
            raise ValueError("Unrecognized open order; reconciliation required")
        if order["symbol"] != symbol:
            continue
        if order["side"] == "buy":
            raise ValueError("Conflicting open BUY")
        try:
            qty, filled = Decimal(str(order["qty"])), Decimal(str(order.get("filled_qty", 0)))
            if not qty.is_finite() or not filled.is_finite() or not 0 <= filled <= qty:
                raise ValueError("Invalid reservation quantities")
            reserved += qty - filled
        except (KeyError, ArithmeticError) as exc:
            raise ValueError("Uncertain SELL reservation") from exc
    return reserved


def sellable(account, symbol):
    held = Decimal(str(account["quantities"].get(symbol, 0)))
    if not held.is_finite() or held < 0:
        raise ValueError("Invalid long position")
    reserved = reserved_sells(account, symbol)
    available = Decimal(str(account.get("available_quantities", {}).get(symbol, held)))
    if not available.is_finite() or available < 0:
        raise ValueError("Invalid available position")
    return max(0, int(min(held - reserved, available).to_integral_value(rounding=ROUND_FLOOR)))


def plan(decision, account, quote, values, policy, risk, symbols, count, paused):
    if paused or not policy.ready():
        raise ValueError("Paused or strategy not configured")
    if decision.symbol not in symbols:
        raise ValueError("Outside watchlist")
    if decision.action == "HOLD":
        return None
    if account["blocked"]:
        raise ValueError("Account blocked")
    equity, baseline = account["equity"], account["last_equity"]
    numbers = [equity, baseline, account["cash"], account["buying_power"], *account["positions"].values(), *account["quantities"].values()]
    if not all(math.isfinite(v) for v in numbers) or equity <= 0 or baseline <= 0 or any(v < 0 for v in [*account["positions"].values(), *account["quantities"].values()]):
        raise ValueError("Unsupported or invalid account state")
    buy = decision.action == "BUY"
    if buy:
        if account["open_orders"]:
            raise ValueError("Open orders block increased exposure")
        if account.get("unpriced_holdings"):
            raise ValueError("Held symbols have invalid data; increased exposure blocked")
        if equity <= baseline * (1 - risk.max_daily_loss) or count >= risk.max_daily_orders:
            raise ValueError("Daily loss/order limit blocks increased exposure")
        if decision.target_weight > risk.max_symbol_weight:
            raise ValueError("Symbol weight limit")
    for check in policy.entry_checks if buy else policy.exit_checks:
        v = values.get(check.indicator)
        if v is None or not math.isfinite(v) or not (v > check.value if check.op == "gt" else v < check.value):
            raise ValueError("User strategy condition failed")
    current = account["positions"].get(decision.symbol, 0)
    delta = decision.target_weight * equity - current
    if (buy and delta <= 0) or (not buy and delta >= 0):
        raise ValueError("Action conflicts with target; SELL must reduce a real long position")
    bid, ask = Decimal(str(quote["bid"])), Decimal(str(quote["ask"]))
    if not bid.is_finite() or not ask.is_finite() or not 0 < bid <= ask:
        raise ValueError("Invalid quote")
    price = (ask if buy else bid).quantize(Decimal("0.01"), rounding=ROUND_CEILING if buy else ROUND_FLOOR)
    if price <= 0:
        raise ValueError("Invalid limit price")
    quantity = int((Decimal(str(abs(delta))) / price).to_integral_value(rounding=ROUND_FLOOR))
    if not buy:
        additional=max(0,int((Decimal(quantity)-reserved_sells(account,decision.symbol)).to_integral_value(rounding=ROUND_FLOOR)))
        quantity = min(additional, sellable(account, decision.symbol))
    if quantity <= 0:
        return None
    cost = quantity * float(price)
    if buy and (cost > min(account["cash"], account["buying_power"]) or
                (sum(account["positions"].values()) + cost) / equity > risk.max_total_weight or
                (current + cost) / equity > risk.max_symbol_weight):
        raise ValueError("Buying power or exposure limit")
    intent = {"symbol": decision.symbol, "side": decision.action.lower(), "quantity": quantity,
              "limit_price": float(price), "type": "limit", "time_in_force": "ioc"}
    validate_order(intent)
    return intent
