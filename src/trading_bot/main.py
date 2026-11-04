import argparse
import json
import logging
from .settings import ROOT, config, credentials
from .storage import Store

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("command",choices=["dry-run","paper","preflight","check","status","pause","resume"])
    parser.add_argument("--mode",choices=["paper","dry-run"],default="paper")
    args=parser.parse_args()
    mode="dry-run" if args.command=="dry-run" else args.mode
    store=Store(ROOT / "data" / f"{mode}.sqlite")
    if args.command in ("pause","resume"):
        store.pause(args.command=="pause"); print(args.command); return
    if args.command=="status":
        from .reporting import report
        print(json.dumps(report(store),ensure_ascii=False,indent=2)); return
    symbols,policy,risk=config()
    if args.command=="preflight":
        from .preflight import run
        run(); return
    if args.command=="check":
        keys=credentials()
        print(json.dumps({"symbols":len(symbols),"strategy_ready":bool(policy.ready()),"credentials_present":{k:bool(v) for k,v in keys.items()},"paper_only":True},indent=2)); return
    (ROOT / "logs").mkdir(exist_ok=True)
    logging.basicConfig(filename=ROOT / "logs/bot.log",level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s")
    import portalocker
    with portalocker.Lock(str(ROOT / "data" / f"{mode}.lock"),timeout=0):
        if args.command=="dry-run":
            from .dry_run import run
            print(run(store,risk)); return
        if mode!="paper":
            raise SystemExit("paper requires --mode paper")
        keys=credentials()
        if not symbols or not policy.ready() or not all(keys.values()):
            raise SystemExit("Blocked: provide watchlist, explicit strategy/checks and all API credentials")
        from .broker import build_broker
        from .strategy import PaperStrategy
        from lumibot.traders import Trader
        trader=Trader()
        trader.add_strategy(PaperStrategy(broker=build_broker(keys),parameters={"store":store}))
        trader.run_all()

if __name__=="__main__":
    try:
        main()
    except Exception as exc:
        # Persist startup errors as well as cycle errors without provider bodies.
        failure_store=Store(ROOT/"data/paper.sqlite")
        failure_store.event("error",{"type":type(exc).__name__,"stage":"process_start_or_runtime"})
        failure_store.db.close()
        print("Robot exited safely: "+type(exc).__name__,file=__import__('sys').stderr)
        raise SystemExit(1)
