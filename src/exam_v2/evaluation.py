"""Pure evaluation functions for a future historical replay; no replay is run here."""
from __future__ import annotations

import math
import statistics


def buy_and_hold(prices):
    values=[float(x) for x in prices]
    if len(values)<2 or any(x<=0 for x in values):raise ValueError("At least two positive prices required")
    return values[-1]/values[0]-1


def equal_weight_pool_baseline(prices_by_symbol):
    """Simple fixed-pool buy-and-hold comparator; explicitly ex-post selected."""
    if not prices_by_symbol:raise ValueError("No pool price series")
    return sum(buy_and_hold(values) for values in prices_by_symbol.values())/len(prices_by_symbol)


def evaluate(equity_curve,closed_trades,exposures,*,qqq_prices=None,pool_prices=None):
    """Separate long, short and whole-book metrics from replay outputs."""
    equity=[float(x) for x in equity_curve]
    if len(equity)<2 or any(x<=0 for x in equity):raise ValueError("At least two positive equity marks")
    returns=[equity[i]/equity[i-1]-1 for i in range(1,len(equity))]
    volatility=statistics.stdev(returns) if len(returns)>1 else 0.0
    sharpe=math.sqrt(252)*statistics.mean(returns)/volatility if volatility>0 else None
    peak=equity[0];max_drawdown=0.0
    for value in equity:
        peak=max(peak,value)
        max_drawdown=max(max_drawdown,1-value/peak)
    trades=list(closed_trades)
    directions={"long":[],"short":[]}
    for trade in trades:
        direction=trade["direction"]
        if direction not in directions:raise ValueError("Unknown trade direction")
        directions[direction].append(trade)
    def summary(items):
        pnl=[float(t["pnl"]) for t in items]
        wins=[x for x in pnl if x>0]
        losses=[-x for x in pnl if x<0]
        return {"trades":len(items),"pnl":sum(pnl),"win_rate":len(wins)/len(items) if items else None,
                "payoff_ratio":(statistics.mean(wins)/statistics.mean(losses)) if wins and losses else None,
                "average_holding_minutes":statistics.mean(float(t["holding_minutes"]) for t in items) if items else None}
    gross=[float(x) for x in exposures]
    if len(gross)!=len(equity) or any(x<0 for x in gross):raise ValueError("Exposure marks must align with equity")
    all_trades=summary(trades)
    return {"total_return":equity[-1]/equity[0]-1,"max_drawdown":max_drawdown,
            "sharpe_252":sharpe,"trades":all_trades["trades"],"win_rate":all_trades["win_rate"],
            "payoff_ratio":all_trades["payoff_ratio"],
            "capital_utilization_mean":statistics.mean(x/e for x,e in zip(gross,equity)),
            "average_holding_minutes":all_trades["average_holding_minutes"],
            "long":summary(directions["long"]),"short":summary(directions["short"]),
            "qqq_buy_hold_return":buy_and_hold(qqq_prices) if qqq_prices is not None else None,
            "fixed_pool_equal_weight_return":equal_weight_pool_baseline(pool_prices) if pool_prices is not None else None,
            "fixed_pool_selection_bias":"current watchlist selected after historical dates"}
