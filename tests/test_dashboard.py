from trading_bot.dashboard import create_app, ORIGIN
from trading_bot.storage import Store

def client(tmp_path,monkeypatch):
    import trading_bot.dashboard as d
    monkeypatch.setattr(d,"worker_processes",lambda:[])
    monkeypatch.setattr(d,"start_worker",lambda:None)
    app=create_app(tmp_path/"web.sqlite")
    return app.test_client()

def test_control_requires_token_and_origin(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch)
    assert c.post("/api/control/resume",base_url=ORIGIN).status_code==403
    assert c.get("/api/status",base_url="http://evil.test").status_code==403

def test_pause_resume_and_unknown_order_block(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch)
    status=c.get("/api/status",base_url=ORIGIN).json
    h={"Origin":ORIGIN,"X-Control-Token":status["control_token"]}
    assert c.post("/api/control/pause",base_url=ORIGIN,headers=h).status_code==200
    assert c.get("/api/status",base_url=ORIGIN).json["paused"] is True
    assert c.post("/api/control/resume",base_url=ORIGIN,headers=h).status_code==200
    store=Store(tmp_path/"web.sqlite");store.reserve("unknown","2026-11-04",{});store.db.close()
    assert c.post("/api/control/stop",base_url=ORIGIN,headers=h).status_code==409
    assert c.get("/api/status",base_url=ORIGIN).json["paused"] is True
    assert c.post("/api/control/resume",base_url=ORIGIN,headers=h).status_code==409

def test_assets_and_status(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch)
    assert c.get("/",base_url=ORIGIN).status_code==200
    assert c.get("/app.js",base_url=ORIGIN).status_code==200
    s=c.get("/api/status",base_url=ORIGIN).json
    assert s["service"]=="ai-paper-trader" and not s["running"]
    assert len(s["symbols"])==48
