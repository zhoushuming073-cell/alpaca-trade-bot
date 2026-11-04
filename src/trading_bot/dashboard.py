"""Loopback-only dashboard and local worker lifecycle controls."""
import json
import os
import re
import secrets
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
import psutil
from flask import Flask, jsonify, request, send_from_directory
from .settings import ROOT, config, credentials
from .storage import Store

PORT = 4200
ORIGIN = f"http://127.0.0.1:{PORT}"

def worker_processes():
    result=[]
    for p in psutil.process_iter(["pid","cmdline","exe"]):
        try:
            cmd=p.info["cmdline"] or []
            exe=p.info["exe"] or ""
            if "trading_bot.main" in cmd and "paper" in cmd and Path(exe).resolve()==(ROOT/".venv/Scripts/python.exe").resolve():
                result.append(p)
        except (psutil.Error,OSError):
            pass
    return result

def worker_environment():
    credentials()  # local .env wins over legacy files
    env=os.environ.copy()
    legacy=ROOT.parent/"api-key.txt"
    if not env.get("DEEPSEEK_API_KEY") and legacy.exists():
        env["DEEPSEEK_API_KEY"]=legacy.read_text(encoding="utf-8-sig").strip()
    legacy=ROOT.parent/"alpaca key.txt"
    if legacy.exists():
        raw=legacy.read_text(encoding="utf-8-sig")
        for label,key in [("Key","ALPACA_API_KEY"),("Secret","ALPACA_API_SECRET")]:
            match=re.search(r"(?im)^"+label+r"[\s:：=]+(\S+)",raw)
            if not env.get(key) and match: env[key]=match[1]
    if not all(env.get(k) for k in ["DEEPSEEK_API_KEY","ALPACA_API_KEY","ALPACA_API_SECRET"]):
        raise ValueError("Missing API credentials")
    env["ALPACA_IS_PAPER"]="true"
    env["PYTHONUTF8"]="1"
    env["LUMIBOT_HIDE_BOTSPOT_TIP"]="1"
    return env

def start_worker():
    if worker_processes(): return
    env=worker_environment()
    (ROOT/"logs").mkdir(exist_ok=True)
    with (ROOT/"logs/worker-console.log").open("ab") as output:
        subprocess.Popen([str(ROOT/".venv/Scripts/python.exe"),"-m","trading_bot.main","paper"],cwd=ROOT,env=env,stdout=output,stderr=subprocess.STDOUT,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))

def snapshot(store):
    def last(kind):
        row=store.db.execute("SELECT ts,payload FROM events WHERE kind=? ORDER BY id DESC LIMIT 1",(kind,)).fetchone()
        return {"ts":row[0],**json.loads(row[1])} if row else None
    account=last("account")
    source="运行快照"
    if account is None and (ROOT/"data/preflight.json").exists():
        preflight=json.loads((ROOT/"data/preflight.json").read_text(encoding="utf-8"))
        if preflight.get("account"):
            account={"ts":preflight["timestamp"],**preflight["account"]}
            source="最近只读接入检查"
    history=[]; peak=0.; drawdown=0.
    for ts,p in store.db.execute("SELECT ts,payload FROM events WHERE kind='account' ORDER BY id"):
        a=json.loads(p); equity=a["equity"]
        peak=max(peak,equity)
        if peak: drawdown=max(drawdown,1-equity/peak)
        history.append({"ts":ts,"equity":equity})
    if not history and account: history=[{"ts":account["ts"],"equity":account["equity"]}]
    symbols,policy,risk=config()
    from .market_data import opening_minutes
    minutes=opening_minutes(datetime.now(timezone.utc))
    window_label="交易窗口内" if minutes is not None and 0<=minutes<30 else "交易窗口外"
    now=datetime.now(timezone.utc)
    age=(now-datetime.fromisoformat(account["ts"])).total_seconds() if account else None
    return {"service":"ai-paper-trader","now":now.isoformat(),"running":bool(worker_processes()),"paused":store.paused(),
        "account":account,"account_source":source,"account_age_seconds":age,"window":window_label,
        "daily_pnl":account["equity"]-account["last_equity"] if account else None,
        "return":account["equity"]/history[0]["equity"]-1 if account and history[0]["equity"] else None,
        "drawdown":drawdown if history else None,"equity_history":history[::max(1,len(history)//300)][-300:],
        "decision":last("decision"),"heartbeat":last("heartbeat"),"error":last("error"),"unresolved":store.pending(),
        "orders":[{"cid":cid,"day":day,"status":status,**json.loads(payload)} for cid,day,status,payload in store.db.execute("SELECT * FROM orders ORDER BY rowid DESC LIMIT 100")],
        "events":[{"ts":ts,"kind":kind,"payload":json.loads(p)} for ts,kind,p in store.db.execute("SELECT ts,kind,payload FROM events WHERE kind NOT IN ('input','account','heartbeat') ORDER BY id DESC LIMIT 40")],
        "cycles":[{"id":i,"status":s} for i,s in store.db.execute("SELECT id,status FROM cycles ORDER BY rowid DESC LIMIT 10")],
        "symbols":symbols,"strategy_version":policy.version,"risk":risk.model_dump()}

def create_app(db_path=None):
    app=Flask(__name__)
    db_path=db_path or ROOT/"data/paper.sqlite"
    token=secrets.token_urlsafe(32)
    mutex=threading.Lock()
    @app.before_request
    def local_only():
        if request.host!=f"127.0.0.1:{PORT}": return jsonify(error="Invalid host"),403
        if request.method=="POST" and (request.headers.get("Origin")!=ORIGIN or not secrets.compare_digest(request.headers.get("X-Control-Token",""),token)):
            return jsonify(error="Invalid control request"),403
    @app.after_request
    def headers(response):
        response.headers["Cache-Control"]="no-store"
        response.headers["X-Content-Type-Options"]="nosniff"
        response.headers["Content-Security-Policy"]="default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; frame-ancestors 'none'"
        return response
    @app.get("/")
    def index(): return send_from_directory(ROOT/"web","index.html")
    @app.get("/app.js")
    def js(): return send_from_directory(ROOT/"web","app.js")
    @app.get("/api/status")
    def status():
        store=Store(db_path)
        try: return jsonify(**snapshot(store),control_token=token)
        finally: store.db.close()
    @app.post("/api/control/<action>")
    def control(action):
        with mutex:
            store=Store(db_path)
            try:
                if action=="pause": store.pause(True)
                elif action=="start": start_worker()
                elif action=="resume":
                    if store.pending(): return jsonify(error="有未确认订单，请先检查纸账户。"),409
                    start_worker()
                    store.pause(False)
                elif action=="stop":
                    store.pause(True)
                    # Prevent disabling cutoff cleanup while orders may still be live.
                    if store.pending(): return jsonify(error="已暂停新订单；仍有未决委托，保留进程以继续核对和截止撤单。"),409
                    for p in worker_processes():
                        try: p.terminate()
                        except psutil.NoSuchProcess: pass
                else: return jsonify(error="Unknown action"),400
                store.event("operator",{"action":action,"source":"local_dashboard"})
                return jsonify(ok=True)
            except Exception as exc:
                return jsonify(error="操作失败："+type(exc).__name__+"；请检查本地日志与配置。"),500
            finally: store.db.close()
    return app

def main():
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument("--start-worker",action="store_true")
    args=parser.parse_args()
    (ROOT/"data").mkdir(exist_ok=True)
    import portalocker
    with portalocker.Lock(str(ROOT/"data/dashboard.lock"),timeout=0):
        if args.start_worker: start_worker()
        from werkzeug.serving import make_server
        make_server("127.0.0.1",PORT,create_app(),threaded=True).serve_forever()

if __name__=="__main__": main()
