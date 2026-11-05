import argparse
import json
import logging
import sys
from pathlib import Path

from .settings import ROOT, config_snapshot, credentials, load_config
from .storage import Store


def _db_path(mode: str) -> Path:
    return ROOT / "data" / f"{mode}.sqlite"


def _mode_from_argv(argv) -> str:
    if "dry-run" in argv:
        return "dry-run"
    if "--mode" in argv:
        try:
            value = argv[argv.index("--mode") + 1]
            if value in {"paper", "dry-run"}:
                return value
        except (ValueError, IndexError):
            pass
    return "paper"


def _install_logging():
    (ROOT / "logs").mkdir(exist_ok=True)
    import time

    from .safety import redact

    logging.Formatter.converter = time.gmtime
    logging.basicConfig(
        filename=ROOT / "logs/bot.log", level=logging.INFO,
        format="%(asctime)sZ %(levelname)s %(message)s",
    )

    class RedactionFilter(logging.Filter):
        def filter(self, record):
            record.msg = redact(record.getMessage())
            record.args = ()
            if record.exc_info:
                record.msg += " | " + redact(str(record.exc_info[1]))
                record.exc_info = None
                record.exc_text = None
            return True

    for handler in logging.getLogger().handlers:
        handler.addFilter(RedactionFilter())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=[
            "dry-run", "paper", "preflight", "check", "status", "pause",
            "resume", "acknowledge-safety", "config", "resolve-order",
            "research-bootstrap",
        ],
    )
    parser.add_argument("--mode", choices=["paper", "dry-run"], default="paper")
    parser.add_argument("--cid", default="")
    parser.add_argument("--reason", default="")
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--symbols", nargs="*", default=[])
    parser.add_argument("--batch", type=int, default=3)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    mode = "dry-run" if args.command == "dry-run" else args.mode
    if args.command == "resolve-order":
        if not args.cid or not args.confirm:
            raise SystemExit(
                "resolve-order requires --cid and --confirm after manually verifying "
                "the broker has no record of that client_order_id"
            )
        store = Store(_db_path(mode), role="operator")
        try:
            ok = store.resolve_unknown_not_sent(
                args.cid, args.reason or "operator verified no broker order by client_order_id"
            )
        finally:
            store.close()
        print("resolved_not_sent" if ok else "not_resolved")
        return

    if args.command == "check":
        bundle = load_config()
        keys = credentials()
        print(json.dumps({
            "symbols": len(bundle.symbols),
            "strategy_ready": bool(bundle.policy.ready()),
            "config_hash": bundle.hash,
            "hashes": bundle.hashes,
            "execution": bundle.execution.model_dump(),
            "broker_stop": "VERIFIED" if (
                bundle.execution.allow_broker_stop and bundle.execution.broker_stop_verified
            ) else "UNVERIFIED",
            "credentials_present": {k: bool(v) for k, v in keys.items()},
            "paper_only": True,
        }, indent=2))
        return

    if args.command == "config":
        bundle = load_config()
        print(json.dumps(config_snapshot(bundle), indent=2, ensure_ascii=False))
        return

    if args.command == "preflight":
        from .preflight import run

        run()
        return

    if args.command == "research-bootstrap":
        # Explicit, bounded local research batch.  It uses the DeepSeek API and
        # therefore is never started automatically by the trading worker.
        from lumibot.entities import Asset

        from .broker import build_broker
        from .deepseek_client import DeepSeek
        from .research import ResearchService
        from .sources import DeepSeekResearchSources, DeepSeekTools

        bundle = load_config()
        keys = credentials()
        if not all(keys.values()):
            raise SystemExit("Blocked: research bootstrap requires configured credentials")
        selected = list(bundle.symbols) if args.all else list(args.symbols)
        if not selected:
            raise SystemExit("Provide --symbols or --all")
        unknown = [symbol for symbol in selected if symbol not in bundle.symbols]
        if unknown:
            raise SystemExit("Outside fixed watchlist: " + ", ".join(unknown))
        broker = build_broker(keys, start_orders_thread=False)

        def data(symbol):
            return broker.data_source.get_historical_prices(
                Asset(symbol), 80, timestep="5 minutes", include_after_hours=False
            ).df

        def daily(symbol):
            return broker.data_source.get_historical_prices(
                Asset(symbol), 160, timestep="day"
            ).df

        store = Store(_db_path("paper"), role="research")
        try:
            ai = DeepSeek(
                keys["DEEPSEEK_API_KEY"], audit=store.event, record=store.ai_call,
                input_record=store.input_event,
                max_tokens=bundle.execution.ai_max_tokens,
                temperature=bundle.execution.ai_temperature,
                repair_attempts=bundle.execution.ai_repair_attempts,
            )
            tools = DeepSeekTools(keys["DEEPSEEK_API_KEY"], audit=store.event)
            service = ResearchService(
                store, ai, data, daily, bundle.execution,
                sources=DeepSeekResearchSources(tools),
            )
            result = service.bootstrap(selected, batch_size=max(1, args.batch), force=args.force)
        finally:
            store.close()
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return

    store = Store(_db_path(mode), role="cli")
    if args.command == "acknowledge-safety":
        store.acknowledge()
        print("Safety acknowledged; trading remains paused")
        return
    if args.command in ("pause", "resume"):
        if args.command == "resume":
            if store.freeze_reason() or store.config_pending():
                raise SystemExit(
                    "Resume blocked: unresolved/unknown order state, safety latch, or config pending restart"
                )
        store.pause(args.command == "pause")
        print(args.command)
        return
    if args.command == "status":
        from .reporting import report

        print(json.dumps(report(store), ensure_ascii=False, indent=2, default=str))
        return

    bundle = load_config()
    if args.command == "dry-run":
        from .dry_run import run

        print(run(store, bundle))
        return

    if args.command == "paper" and mode != "paper":
        raise SystemExit("paper requires --mode paper")
    keys = credentials()
    if not bundle.symbols or not bundle.policy.ready() or not all(keys.values()):
        raise SystemExit("Blocked: provide watchlist, explicit strategy/checks and all API credentials")

    import portalocker

    _install_logging()
    with portalocker.Lock(str(ROOT / "data" / f"{mode}.lock"), timeout=0):
        from lumibot.traders import Trader

        from .broker import build_broker
        from .strategy import PaperStrategy

        trader = Trader()
        trader.add_strategy(
            PaperStrategy(broker=build_broker(keys), parameters={"store": store})
        )
        trader.run_all()


if __name__ == "__main__":
    mode = _mode_from_argv(sys.argv)
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        failure_store = None
        try:
            failure_store = Store(_db_path(mode), role="startup-failure")
            failure_store.error("process_start_or_runtime", exc)
        finally:
            if failure_store is not None:
                failure_store.close()
        print("Robot exited safely: " + type(exc).__name__, file=sys.stderr)
        raise SystemExit(1)
