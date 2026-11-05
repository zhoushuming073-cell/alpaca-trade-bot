"""Read-only external validation: no submit/cancel paths are called."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

from .safety import redact
from .settings import ROOT, credentials, load_config


def run():
    from lumibot.entities import Asset

    from .broker import PaperGateway, build_broker
    from .market_data import daily_history

    bundle = load_config()
    broker = build_broker(credentials(), start_orders_thread=False)
    gate = PaperGateway(
        SimpleNamespace(broker=broker), symbols=bundle.symbols, execution=bundle.execution
    )
    result = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "endpoint": "https://paper-api.alpaca.markets",
        "paper_only": True,
        "strategy_ready": bool(bundle.policy.ready()),
        "symbols": len(bundle.symbols),
        "config_hash": bundle.hash,
        "hashes": bundle.hashes,
        "execution": bundle.execution.model_dump(),
        "broker_stop": "VERIFIED" if (
            bundle.execution.allow_broker_stop and bundle.execution.broker_stop_verified
        ) else "UNVERIFIED",
        "order_execution_tested": False,
        "paper_order_lifecycle_verified": False,
    }
    try:
        result["account"] = gate.snapshot()
        result["market_open"] = gate.is_open()
    except Exception as exc:  # noqa: BLE001
        result["account_error"] = {
            "type": type(exc).__name__, "stage": "alpaca_snapshot",
            "reason": redact(str(exc)), "outcome": "preflight_blocked",
        }
        result["ready"] = False
        (ROOT / "data/preflight.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    issues = []
    for symbol in bundle.symbols:
        try:
            gate.validate_assets([symbol])
        except Exception as exc:  # noqa: BLE001
            issues.append({
                "symbol": symbol, "error": type(exc).__name__, "stage": "asset_validation",
                "reason": redact(str(exc)),
            })
    result["asset_issues"] = issues
    try:
        symbol = next(s for s in bundle.symbols if s not in {i["symbol"] for i in issues})
        bars = broker.data_source.get_historical_prices(
            Asset(symbol), 80, timestep="5 minutes", include_after_hours=False
        ).df
        daily = daily_history(
            broker.data_source.get_historical_prices(Asset(symbol), 160, timestep="day").df,
            datetime.now(timezone.utc),
        )
        result["market_data"] = {
            "symbol": symbol, "feed": "iex", "intraday_rows": len(bars),
            "last_intraday_start": str(bars.index[-1]),
            "daily_rows": len(daily), "last_daily": str(daily.index[-1]),
            "volume_scope": "IEX only",
        }
    except Exception as exc:  # noqa: BLE001
        result["data_error"] = {
            "type": type(exc).__name__, "stage": "market_data",
            "reason": redact(str(exc)), "outcome": "preflight_blocked",
        }
    result["read_only_ready"] = not issues and "data_error" not in result
    result["ready"] = result["read_only_ready"]
    result["execution_ready"] = False  # requires a separately authorized Paper lifecycle test
    (ROOT / "data/preflight.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
