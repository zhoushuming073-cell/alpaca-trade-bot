"""Read-only external validation: no submit/cancel paths."""
from datetime import datetime, timezone
import json
from .settings import ROOT, credentials, config

def run():
    from .broker import build_broker, PaperGateway
    from lumibot.entities import Asset
    from types import SimpleNamespace
    symbols,policy,risk=config()
    broker=build_broker(credentials(),start_orders_thread=False)
    gate=PaperGateway(SimpleNamespace(broker=broker))
    result={"timestamp":datetime.now(timezone.utc).isoformat(),"endpoint":"https://paper-api.alpaca.markets","strategy_ready":bool(policy.ready()),"symbols":len(symbols)}
    try:
        result["account"]=gate.snapshot()
        result["market_open"]=gate.is_open()
    except Exception as exc:
        result["account_error"]=type(exc).__name__
        result["ready"]=False
        (ROOT/"data/preflight.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
        print(json.dumps(result,ensure_ascii=False,indent=2)); return
    issues=[]
    for symbol in symbols:
        try:
            gate.validate_assets([symbol])
        except Exception as exc:
            issues.append({"symbol":symbol,"error":type(exc).__name__})
    result["asset_issues"]=issues
    try:
        from .market_data import daily_history
        symbol=next(s for s in symbols if s not in {i['symbol'] for i in issues})
        bars=broker.data_source.get_historical_prices(Asset(symbol),42,timestep="5 minutes",include_after_hours=False).df
        daily=daily_history(broker.data_source.get_historical_prices(Asset(symbol),160,timestep="day").df,datetime.now(timezone.utc))
        result["market_data"]={"symbol":symbol,"feed":"iex","intraday_rows":len(bars),"last_intraday_start":str(bars.index[-1]),"daily_rows":len(daily),"last_daily":str(daily.index[-1])}
    except Exception as exc:
        result["data_error"]=type(exc).__name__
    result["ready"]=not issues and "data_error" not in result
    (ROOT/"data/preflight.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(result,ensure_ascii=False,indent=2))
