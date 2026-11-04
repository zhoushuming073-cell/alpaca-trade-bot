import hashlib

def reconcile(store, broker):
    for cid in store.pending():
        found=broker.lookup(cid)
        if found:
            store.order(cid,found)
    account=broker.snapshot()
    store.event("account",account)
    return account

def execute(store, broker, cycle, day, intent):
    cid="aip-"+hashlib.sha256(f"{cycle}:{intent['symbol']}".encode()).hexdigest()[:36]
    # Durable reservation precedes any network mutation. Unknown is never retried.
    store.reserve(cid,day,intent)
    try:
        broker.submit(intent,cid)
    except Exception as exc:
        store.event("submission_error",{"cid":cid,"type":type(exc).__name__})
    try:
        found=broker.lookup(cid)
        if found:
            store.order(cid,found)
    except Exception as exc:
        store.event("lookup_error",{"cid":cid,"type":type(exc).__name__})
    return cid
