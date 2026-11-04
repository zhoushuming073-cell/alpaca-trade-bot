"""Independent cutoff cancellation; AI calls cannot block this worker."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Event, Thread
from .market_data import opening_minutes
from .storage import Store

class CutoffWatchdog:
    def __init__(self,path,broker):
        self.path,self.broker=path,broker
        self.stop_event=Event()
        self.thread=Thread(target=self.run,name="cutoff-watchdog",daemon=True)
    def start(self): self.thread.start()
    def stop(self): self.stop_event.set()
    def run(self):
        store=Store(self.path)
        try:
            while not self.stop_event.wait(1):
                minutes=opening_minutes(datetime.now(timezone.utc))
                if minutes is None or not 29.5<=minutes<30:
                    continue
                pending=store.pending()
                if not pending: continue
                with ThreadPoolExecutor(max_workers=10) as pool:
                    tasks={cid:pool.submit(self.broker.cancel,cid) for cid in pending}
                    for cid,task in tasks.items():
                        try:
                            task.result()
                            found=self.broker.lookup(cid)
                            if found: store.order(cid,found)
                        except Exception as exc:
                            store.event("cutoff_error",{"cid":cid,"type":type(exc).__name__})
                if store.pending():
                    store.pause(True)
                    store.event("critical_cutoff_pending",{"orders":store.pending()})
        finally:
            store.db.close()
