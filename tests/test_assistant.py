import json
from datetime import datetime, timezone

import pytest

from trading_bot.assistant import Companion, AssistantError, AssistantBusy, concise
from trading_bot.dashboard import create_app, ORIGIN
from trading_bot.storage import Store


@pytest.fixture
def setup(store, clock):
    status = {"running": True, "paused": False, "phase": "ACTIVE", "symbols": ["IREN", "RIOT"],
              "account": {"quantities": {"IREN": 3}}, "account_stale": False, "account_age_seconds": 1,
              "health": {"worker": {"alive": True, "stale": False}}, "plans": [], "orders": [],
              "research": [{"symbol": "IREN", "status": "OK"}], "control_token": "DO_NOT_SEND"}
    calls = []
    def model(payload):
        calls.append(payload)
        return {"should_speak": True, "answer": "记录显示机器人正在等待计划条件满足。目前没有新的成交记录；缺少实时价格，不能判断是否接近买点。", "usage": {"model": "fake"}}
    assistant = Companion(store.path, lambda: status, model=model, clock=clock)
    return assistant, status, calls


def event(store, kind="order", **extra):
    return store.event(kind, {"symbol": "IREN", "status": "filled", "cid": "aip-test", "filled_qty": "3", **extra})


def test_chat_uses_records_and_persists_without_controls(setup, store):
    a, status, calls = setup
    before = list(store.db.execute("SELECT * FROM controls"))
    assert a.ask("机器人正在做什么？", "request-0001")["status"] == "done"
    assert calls[0]["system_records"]["positions"] == {"IREN": 3}
    assert "DO_NOT_SEND" not in json.dumps(calls)
    assert [m["role"] for m in a.history()["messages"]] == ["user", "assistant"]
    assert list(store.db.execute("SELECT * FROM controls")) == before
    assert store.db.execute("SELECT count(*) FROM orders").fetchone()[0] == 0


def test_idempotent_question_does_not_bill_twice(setup):
    a, _, calls = setup
    a.ask("现在如何", "same-request")
    a.ask("现在如何", "same-request")
    assert len(calls) == 1 and len(a.history()["messages"]) == 2


def test_persistent_history_after_recreation(setup, store, clock):
    a, status, calls = setup
    a.ask("状态", "request-0001")
    reopened = Companion(store.path, lambda: status, model=lambda x: pytest.fail("must not call model"), clock=clock)
    assert len(reopened.history()["messages"]) == 2
    assert len(reopened.history(after=1)["messages"]) == 1
    assert len(reopened.history(before=2)["messages"]) == 1


def test_context_loads_named_stock_and_timestamped_news(setup, store, clock):
    a, status, _ = setup
    store.save_research_profile({"symbol": "RIOT", "version": 1, "status": "STALE",
        "facts": ["已记录财报"], "interpretations": ["待验证判断"], "source_status": "AVAILABLE",
        "expires_at_utc": "2026-01-01T00:00:00+00:00"}, evidence=[{
        "kind": "news", "source_url": "https://example.com/news", "title": "已保存新闻",
        "published_at": "2026-11-04T00:00:00+00:00", "retrieved_at": clock().isoformat(), "verified": True,
        "summary": "原文资料"}])
    ctx = a.context(status, "RIOT 目前怎么看？")
    assert ctx["research_details"][0]["symbol"] == "RIOT"
    assert ctx["research_details"][0]["stale"]
    assert ctx["recorded_news"][0]["source_url"].startswith("https://")
    assert "未获取新的联网新闻" in ctx["news_note"]


def test_local_cutoff_is_not_stock_market_close(setup, clock):
    a, status, _ = setup
    clock.set(datetime(2026, 11, 5, 16, 15, tzinfo=timezone.utc))
    status["phase"] = "CLOSED"
    ctx = a.context(status)
    assert "尚未收盘" in ctx["market_session"]
    assert "主动交易窗口已结束" in ctx["local_activity"]
    assert "phase" not in ctx


def test_default_reply_limit_and_expanded_detail():
    text = "这是根据真实记录整理的市场观察与持仓说明，实际决策仍以最新计划为准。" * 15
    assert 30 <= len(concise(text)) <= 150
    assert concise(text).endswith("。")
    assert len(concise(text, True)) <= 400
    assert len(concise(text, True)) > len(concise(text))


def test_failed_api_has_explicit_error_not_fake_reply(setup, store):
    a, _, calls = setup
    a.model = lambda p: (_ for _ in ()).throw(TimeoutError("Bearer PRIVATE"))
    with pytest.raises(AssistantError):
        a.ask("状态", "request-0001")
    history = a.history()["messages"]
    assert history[-1]["role"] == "system" and "PRIVATE" not in str(history)
    assert a.ask("状态", "request-0001")["status"] == "failed"


def test_idle_proactive_never_calls_ai(setup):
    a, _, calls = setup
    assert a.proactive_once() == "quiet" and calls == []


def test_fill_notifies_once_when_browser_closed_and_survives_restart(setup, store, clock):
    a, status, calls = setup
    event(store)
    assert a.proactive_once() == "sent"
    event(store)  # repeated broker polling must not create duplicate news
    clock.advance(61)
    assert a.proactive_once() == "quiet"
    reopened = Companion(store.path, lambda: status, model=a.model, clock=clock)
    assert reopened.history()["messages"][0]["source"] == "proactive"
    assert reopened.proactive_once() == "quiet" and len(calls) == 1


def test_ordinary_events_coalesce_and_cool_down(setup, store, clock):
    a, _, calls = setup
    event(store, "quote_errors", errors=[{"symbol": "RIOT", "reason": "spread"}])
    assert a.proactive_once() == "batching"
    event(store, "quote_errors", errors=[{"symbol": "IREN", "reason": "spread"}])
    clock.advance(61)
    assert a.proactive_once() == "sent"
    assert len(calls[0]["system_records"]["trigger_events"]) == 2
    event(store, "quote_errors", errors=[{"symbol": "AAPL", "reason": "spread"}])
    a.collect_events()
    clock.advance(61)
    assert a.proactive_once() == "cooldown" and len(calls) == 1


@pytest.mark.parametrize("change,reason", [({"running": False}, "worker_offline"),
                                          ({"account_stale": True}, "data_unavailable")])
def test_no_proactive_analysis_without_worker_or_fresh_data(setup, store, change, reason):
    a, status, calls = setup
    status.update(change); event(store)
    assert a.proactive_once() == reason and not calls


def test_actual_risk_record_can_explain_outage_without_fresh_account(setup, store):
    a, status, calls = setup
    status.update(account_stale=True, account_error="outage", paused=False)
    event(store, "safety_alert", reason="NETWORK_UNAVAILABLE")
    assert a.proactive_once() == "sent"
    assert calls[0]["system_records"]["account_stale"] is True
    assert calls[0]["system_records"]["paused"] is False


def test_model_can_remain_silent(setup, store):
    a, _, _ = setup
    a.model = lambda p: {"should_speak": False, "answer": ""}
    event(store)
    assert a.proactive_once() == "silent"
    assert not a.history()["messages"]
    assert a.proactive_once() == "quiet"


def test_retry_budget_and_daily_cap(setup, store, clock):
    a, _, calls = setup
    attempts = []
    def failure(p):
        attempts.append(1)
        raise TimeoutError()
    a.model = failure
    event(store)
    for _ in range(3):
        assert a.proactive_once() == "retry_later"
        clock.advance(301)
    assert a.proactive_once() == "quiet" and len(attempts) == 3
    event(store, cid="another")
    with a.db() as db:
        a.set_meta(db, "proactive_calls:2026-11-05", a.DAILY_PROACTIVE_LIMIT)
    assert a.proactive_once() == "budget"


def test_busy_assistant_does_not_block_trading_or_repeat_requests(setup):
    a, _, calls = setup
    a.model_lock.acquire()
    try:
        with pytest.raises(AssistantBusy): a.ask("hi", "request-0001")
        assert calls == []
    finally:
        a.model_lock.release()


def test_api_auth_input_and_readonly_behavior(tmp_path, monkeypatch):
    import trading_bot.dashboard as dashboard
    monkeypatch.setattr(dashboard, "worker_processes", lambda: [])
    monkeypatch.setattr(dashboard, "start_worker", lambda: pytest.fail("chat cannot start worker"))
    monkeypatch.setattr(dashboard, "terminate_workers", lambda: pytest.fail("chat cannot stop worker"))
    calls = []
    def model(p):
        calls.append(p)
        return {"should_speak": True, "answer": "这里只能解读已有记录，不能为你买入股票。"}
    app = create_app(tmp_path / "web.sqlite", companion_model=model)
    c = app.test_client()
    data = c.get("/api/status", base_url=ORIGIN).get_json()
    headers = {"Origin": ORIGIN, "X-Control-Token": data["control_token"]}
    assert c.post("/api/assistant", base_url=ORIGIN, json={}).status_code == 403
    assert c.post("/api/assistant", base_url=ORIGIN, headers=headers, json={"message": "x"*501}).status_code == 400
    assert c.get("/api/assistant?after=-1", base_url=ORIGIN).status_code == 400
    response = c.post("/api/assistant", base_url=ORIGIN, headers=headers,
                      json={"message": "帮我买100股IREN", "request_id": "request-0001"})
    assert response.status_code == 200 and len(calls) == 1
    assert c.get("/api/status", base_url=ORIGIN).get_json()["paused"] == data["paused"]
    assert len(c.get("/api/assistant", base_url=ORIGIN).get_json()["messages"]) == 2
