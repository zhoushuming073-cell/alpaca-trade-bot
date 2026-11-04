import math
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING

def plan(decision, account, quote, values, policy, risk, symbols, count, paused):
    if paused or not policy.ready():
        raise ValueError("Paused or strategy not configured")
    if decision.symbol not in symbols:
        raise ValueError("Outside watchlist")
    if decision.action == "HOLD":
        return None
    if account["open_orders"] or account["blocked"]:
        raise ValueError("Open orders or account blocked")
    equity, baseline = account["equity"], account["last_equity"]
    numbers=[equity,baseline,account["cash"],account["buying_power"],*account["positions"].values(),*account["quantities"].values()]
    if not all(math.isfinite(v) for v in numbers) or equity<=0 or baseline<=0 or any(v<0 for v in account["positions"].values()):
        raise ValueError("Unsupported or invalid account state")
    if equity <= baseline*(1-risk.max_daily_loss) or count >= risk.max_daily_orders:
        raise ValueError("Daily loss/order limit")
    if decision.target_weight > risk.max_symbol_weight:
        raise ValueError("Symbol weight limit")
    checks=policy.entry_checks if decision.action=="BUY" else policy.exit_checks
    for check in checks:
        v=values.get(check.indicator)
        if v is None or not math.isfinite(v) or not (v>check.value if check.op=="gt" else v<check.value):
            raise ValueError("User strategy condition failed")
    current=account["positions"].get(decision.symbol,0)
    delta=decision.target_weight*equity-current
    if (decision.action=="BUY" and delta<=0) or (decision.action=="SELL" and delta>=0):
        raise ValueError("Action conflicts with target")
    buy=decision.action=="BUY"
    price=Decimal(str(quote["ask"] if buy else quote["bid"])).quantize(Decimal("0.01"),rounding=ROUND_CEILING if buy else ROUND_FLOOR)
    if price <= 0:
        raise ValueError("Invalid limit price")
    quantity=float((Decimal(str(abs(delta)))/price).quantize(Decimal("0.000001"),rounding=ROUND_FLOOR))
    if not buy:
        quantity=min(quantity,account["quantities"].get(decision.symbol,0))
    if quantity<=0:
        return None
    cost=quantity*float(price)
    if buy and (cost>min(account["cash"],account["buying_power"]) or (sum(account["positions"].values())+cost)/equity>risk.max_total_weight or (current+cost)/equity>risk.max_symbol_weight):
        raise ValueError("Buying power or exposure limit")
    return {"symbol":decision.symbol,"side":decision.action.lower(),"quantity":quantity,"limit_price":float(price)}
