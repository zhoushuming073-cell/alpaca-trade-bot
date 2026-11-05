import json
from datetime import datetime
from zoneinfo import ZoneInfo

def report(store):
    rows=list(store.db.execute("SELECT ts,kind,payload FROM events ORDER BY id"))
    accounts=[json.loads(p) for _,k,p in rows if k=="account"]
    latest=accounts[-1] if accounts else None
    peak=0.; drawdown=0.
    for a in accounts:
        peak=max(peak,a["equity"])
        if peak: drawdown=max(drawdown,1-a["equity"]/peak)
    def last(kind):
        return next(({"ts":t,**json.loads(p)} for t,k,p in reversed(rows) if k==kind),None)
    return {"paused":store.paused(),"safety_reason":store.safety_reason(),"latest_account":latest,"daily_pnl":latest["equity"]-latest["last_equity"] if latest else None,"return_since_first_snapshot":latest["equity"]/accounts[0]["equity"]-1 if latest and accounts[0]["equity"] else None,"max_observed_drawdown":drawdown,"last_decision":last("decision"),"heartbeat":last("heartbeat"),"latest_error":last("error"),"orders":[{**json.loads(p),"status":s} for p,s in store.db.execute("SELECT payload,status FROM orders")],"unresolved":store.pending()}
