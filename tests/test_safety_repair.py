import json
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
import pandas as pd
import pytest

from trading_bot.dry_run import FakeBroker, HoldAI, bars, fixture_policy
from trading_bot.engine import Engine
from trading_bot.execution import execute, monitor, reconcile
from trading_bot.risk_manager import plan, sellable
from trading_bot.safety import validate_order, IOCUnavailable
from trading_bot.storage import Store
from test_system import decision, engine, BuyAI

NOW = datetime(2026, 11, 2, 14, 35, 10, tzinfo=timezone.utc)
INTENT = {"symbol": "TEST", "quantity": 10, "side": "buy", "limit_price": 100,
          "time_in_force": "ioc", "type": "limit"}


def risk_plan(risk, d=None, account=None, count=0):
    return plan(d or decision(), account or FakeBroker().snapshot(), {"bid": 100, "ask": 100},
                {"close": 100}, fixture_policy(), risk, ["TEST"], count, False)


@pytest.mark.parametrize("limit", ["loss", "count", "overweight", "total_exposure", "all"])
def test_caps_allow_real_reduction(risk, limit):
    a = FakeBroker().snapshot()
    a.update(positions={"TEST": 20000}, quantities={"TEST": 200})
    count = risk.max_daily_orders if limit in ("count", "all") else 0
    if limit in ("loss", "all"):
        a["last_equity"] = 110000
    if limit in ("total_exposure", "all"):
        a["positions"]["OTHER"] = 60000
        a["quantities"]["OTHER"] = 600
    order = risk_plan(risk, decision("SELL", .15), a, count)
    assert order["quantity"] == 50 and order["side"] == "sell"


@pytest.mark.parametrize("qty", [0, -1, .25, 1.01, "1.5", True, float("nan"), float("inf")])
def test_invalid_quantity_at_execution_entry(store, qty):
    broker = FakeBroker()
    with pytest.raises(ValueError):
        execute(store, broker, "cycle", "2026-11-02", {**INTENT, "quantity": qty})
    assert broker.submissions == 0 and store.count("2026-11-02") == 0


@pytest.mark.parametrize("extra", [{"notional": 100}, {"notional": None}, {"time_in_force": "day"},
                                  {"type": "market"}, {"order_class": "bracket"}, {"extended_hours": True}])
def test_forbidden_order_shapes(store, extra):
    b = FakeBroker()
    with pytest.raises(ValueError):
        execute(store, b, "cycle", "2026-11-02", {**INTENT, **extra})
    assert b.submissions == 0


@pytest.mark.parametrize("weight,qty", [(0.00555, 5), (0.00099, 0), (.00001, 0)])
def test_amount_floored_never_rounded_up(risk, weight, qty):
    result = risk_plan(risk, decision("BUY", weight))
    assert (result["quantity"] if result else 0) == qty
    if result:
        assert isinstance(result["quantity"], int)


@pytest.mark.parametrize("quantity,expected", [(10.9, 10), (.9, 0)])
def test_fractional_residual_never_sold(risk, quantity, expected):
    a = FakeBroker().snapshot()
    a.update(positions={"TEST": quantity * 100}, quantities={"TEST": quantity})
    result = risk_plan(risk, decision("SELL", 0), a)
    assert (result["quantity"] if result else 0) == expected


@pytest.mark.parametrize("target", [.2, .3])
def test_sell_cannot_increase_or_preserve_exposure(risk, target):
    a = FakeBroker().snapshot()
    a.update(positions={"TEST": 20000}, quantities={"TEST": 200})
    with pytest.raises(ValueError):
        risk_plan(risk, decision("SELL", target), a)


def test_no_real_shares_means_no_sell(risk):
    a = FakeBroker().snapshot()
    a.update(positions={"TEST": 1000}, quantities={"TEST": 0})
    assert risk_plan(risk, decision("SELL", 0), a) is None


@pytest.mark.parametrize("available,expected", [(None, 4), (3.9, 3), (4.8, 4), (0, 0)])
def test_open_sell_reserves_remaining_not_original_qty(risk, available, expected):
    a = FakeBroker().snapshot()
    a.update(positions={"TEST": 1090}, quantities={"TEST": 10.9},
             open_orders=[{"symbol": "TEST", "side": "sell", "qty": "8", "filled_qty": "2", "status": "partially_filled"}])
    if available is not None:
        a["available_quantities"] = {"TEST": available}
    result = risk_plan(risk, decision("SELL", 0), a)
    assert (result["quantity"] if result else 0) == expected


def test_conflicting_buy_prevents_sell(risk):
    a = FakeBroker().snapshot()
    a.update(positions={"TEST": 1000}, quantities={"TEST": 10},
             open_orders=[{"symbol": "TEST", "side": "buy", "qty": "1", "status": "new"}])
    with pytest.raises(ValueError):
        risk_plan(risk, decision("SELL", 0), a)


@pytest.mark.parametrize("outcome,filled,status", [("filled", 10, "filled"), ("partial", 2, "canceled"),
                                                  ("none", 0, "canceled"), ("rejected", 0, "rejected")])
def test_ioc_outcomes_preserve_fills(store, outcome, filled, status):
    b = FakeBroker(outcome)
    cid = execute(store, b, "cycle", "2026-11-02", INTENT)
    assert not store.pending()
    assert b.orders[cid]["status"] == status
    assert b.positions["TEST"] == filled
    assert store.db.execute("SELECT count(*) FROM fills").fetchone()[0] == (1 if filled else 0)
    assert store.request(cid)["request"]["time_in_force"] == "ioc"
    assert bool(store.safety_reason()) == (outcome == "rejected")


def test_ioc_unavailable_latches_no_fallback(store):
    class Unsupported(FakeBroker):
        def submit(self, intent, cid):
            self.submissions += 1
            assert intent["time_in_force"] == "ioc"
            raise IOCUnavailable("Paper IOC is unavailable")
    b = Unsupported()
    execute(store, b, "cycle", "2026-11-02", INTENT)
    assert store.safety_reason() and store.paused() and b.submissions == 1
    with pytest.raises(ValueError):
        execute(store, b, "next-cycle", "2026-11-02", INTENT)
    assert b.submissions == 1


def test_post_timeout_created_order_restart_never_reposts(store):
    class Timeout(FakeBroker):
        def submit(self, intent, cid):
            super().submit(intent, cid)
            raise TimeoutError("POST response lost")
    b = Timeout()
    cid = execute(store, b, "cycle", "2026-11-02", INTENT)
    restarted = Store(store.path)
    try:
        assert execute(restarted, b, "cycle", "2026-11-02", INTENT) == cid
        assert b.submissions == 1 and not restarted.pending()
        assert reconcile(restarted, b)["quantities"]["TEST"] == 10
    finally:
        restarted.db.close()


@pytest.mark.parametrize("found", [None, {"status": "pending_cancel", "filled_qty": "2"}])
def test_cancel_not_confirmed_is_never_marked_canceled(store, found):
    b = FakeBroker()
    store.reserve("aip-test", "2026-11-02", INTENT, "cycle")
    if found:
        b.orders["aip-test"] = found
    def timeout(cid):
        raise TimeoutError("DELETE timed out")
    b.cancel = timeout
    monitor(store, b, cancel=True)
    assert store.pending() == ["aip-test"] and store.safety_reason()
    assert store.db.execute("SELECT status FROM orders").fetchone()[0] == "reconciliation_required"


def test_cancel_timeout_but_broker_did_cancel(store):
    b = FakeBroker()
    store.reserve("aip-test", "2026-11-02", INTENT)
    b.orders["aip-test"] = {"status": "new"}
    def timeout(cid):
        b.orders[cid]["status"] = "canceled"
        raise TimeoutError("lost acknowledgment")
    b.cancel = timeout
    monitor(store, b, cancel=True)
    assert not store.pending()
    assert store.db.execute("SELECT status FROM orders").fetchone()[0] == "canceled"


def test_disconnect_triggers_alert_and_restart_recovery(store):
    b = FakeBroker()
    store.reserve("aip-test", "2026-11-02", INTENT, "cycle")
    def offline(cid):
        raise ConnectionError("connection lost")
    b.lookup = offline
    monitor(store, b, cancel=True)
    assert store.safety_reason() and store.pending()
    assert store.db.execute("SELECT count(*) FROM events WHERE kind='safety_alert'").fetchone()[0] >= 1
    restarted = Store(store.path)
    b.lookup = lambda cid: {"status": "filled", "filled_qty": "10", "filled_avg_price": "100"}
    monitor(restarted, b)
    assert not restarted.pending() and restarted.paused()  # never auto-resume
    restarted.db.close()


def test_crash_after_reservation_before_post_never_reposts(store):
    import hashlib
    cid = "aip-" + hashlib.sha256(b"cycle:TEST").hexdigest()[:36]
    store.reserve(cid, "2026-11-02", INTENT, "cycle")
    b = FakeBroker()
    execute(store, b, "cycle", "2026-11-02", INTENT)
    assert b.submissions == 0 and store.pending() == [cid] and store.safety_reason()


@pytest.mark.parametrize("status", ["accepted", "pending_new", "new", "partially_filled", "pending_cancel",
                                   "done_for_day", "replaced", "pending_replace", "suspended", "calculated", "alien_status"])
def test_nonterminal_status_never_disappears(store, status):
    store.reserve("cid", "day", INTENT)
    store.order("cid", {"status": status})
    assert store.pending() == ["cid"]


def test_out_of_order_poll_cannot_erase_fills(store):
    store.reserve("cid", "day", INTENT, "cycle")
    store.order("cid", {"status": "partially_filled", "filled_qty": "2"})
    store.order("cid", {"status": "canceled", "filled_qty": "2"})
    store.order("cid", {"status": "new", "filled_qty": "0"})
    store.order("cid", {"status": "partially_filled", "filled_qty": "2"})
    assert not store.pending()
    assert store.db.execute("SELECT count(*) FROM fills").fetchone()[0] == 1


@pytest.mark.parametrize("time", ["2026-11-02T14:59:30Z", "2026-11-02T14:59:59Z", "2026-11-02T15:00:00Z", "2026-11-02T18:00:00Z", "2026-11-07T15:00:00Z"])
def test_safety_cancel_all_times_while_paused(store, risk, time):
    e = engine(store, risk)
    e.clock = lambda: pd.Timestamp(time)
    store.pause(True)
    store.reserve("aip-test", "2026-11-02", INTENT)
    e.broker.orders["aip-test"] = {"status": "new"}
    e.tick()
    assert e.broker.cancel_calls == ["aip-test"]
    assert not store.pending() and e.broker.submissions == 0
    assert e.broker.snapshot_calls == 0


def test_no_remote_poll_when_idle_or_duplicate(store, risk):
    e = engine(store, risk)
    assert e.tick() == "complete"
    counts = e.broker.snapshot_calls, e.broker.clock_calls
    for _ in range(10):
        assert e.tick() == "duplicate"
    assert (e.broker.snapshot_calls, e.broker.clock_calls) == counts
    e.clock = lambda: pd.Timestamp("2026-11-02T18:00:00Z")
    for _ in range(10):
        e.tick()
    assert (e.broker.snapshot_calls, e.broker.clock_calls) == counts


def test_paused_preflight_has_zero_account_reads(store, risk):
    e = engine(store, risk)
    store.pause(True)
    e.tick()
    assert e.broker.snapshot_calls == e.broker.clock_calls == 0


@pytest.mark.parametrize("invalid", [1, 3, 48])
def test_symbol_isolation_48_stocks(store, risk, invalid):
    class Capture(HoldAI):
        payload = None
        def decide(self, payload):
            self.payload = payload
            return super().decide(payload)
    ai = Capture()
    names = [f"T{i}" for i in range(48)]
    def data(s):
        if int(s[1:]) < invalid:
            raise ValueError("Missing completed bar: missing_count=1")
        return bars()
    e = Engine(store, FakeBroker(), ai, names, fixture_policy(), risk, data, clock=lambda: NOW)
    result = e.tick()
    assert result == ("skipped_no_valid_data" if invalid == 48 else "complete")
    if invalid < 48:
        assert len(ai.payload["symbols"]) == 48-invalid
        assert len(ai.payload["excluded_symbols"]) == invalid
    else:
        assert ai.payload is None
    assert store.db.execute("SELECT count(*) FROM events WHERE kind='error'").fetchone()[0] == invalid


def daily_bars():
    from trading_bot.market_data import CALENDAR
    schedule = CALENDAR.schedule("2026-05-02", "2026-11-01")
    index = pd.DatetimeIndex(schedule.index).tz_localize("America/New_York")
    return pd.DataFrame({"open": 100., "high": 101., "low": 99., "close": 100., "volume": 10}, index=index)


@pytest.mark.parametrize("bad_stage", ["intraday_data", "daily_data", "both"])
def test_daily_and_intraday_fail_independently(store, risk, bad_stage):
    def minutes(s):
        if bad_stage in ("intraday_data", "both"):
            return bars().iloc[:0]
        return bars()
    def days(s):
        if bad_stage in ("daily_data", "both"):
            return daily_bars().iloc[:0]
        return daily_bars()
    e = engine(store, risk)
    e.data, e.daily_data = minutes, days
    assert e.tick() == "skipped_no_valid_data"
    errors = [json.loads(r[0])["stage"] for r in store.db.execute("SELECT payload FROM events WHERE kind='error'")]
    assert errors == (["intraday_data", "daily_data"] if bad_stage == "both" else [bad_stage])


@pytest.mark.parametrize("action,expected_submissions", [("BUY", 0), ("SELL", 1)])
def test_missing_held_stock_freezes_asset_blocks_only_increase(store, risk, action, expected_submissions):
    class AI:
        def decide(self, payload):
            assert payload["symbols"] == ["TEST"]
            assert payload["frozen_holdings"] == ["BAD"]
            assert "BAD" in payload["account"]["quantities"]
            return [decision(action, .05 if action == "BUY" else 0)], {}
    e = engine(store, risk, AI())
    e.symbols = ["TEST", "BAD"]
    e.broker.positions = {"TEST": 10, "BAD": 10}
    e.broker.cash = 98000
    e.data = lambda s: bars() if s == "TEST" else bars().iloc[:0]
    assert e.tick() == "complete"
    assert e.broker.submissions == expected_submissions
    assert e.broker.positions["BAD"] == 10


@pytest.mark.parametrize("kind", ["unknown_symbol", "duplicate_decision", "excess_weight"])
def test_ai_cannot_bypass_python(store, risk, kind):
    class AI:
        def decide(self, payload):
            if kind == "unknown_symbol":
                return [decision(symbol="OUTSIDE")], {}
            if kind == "duplicate_decision":
                return [decision(), decision()], {}
            return [decision(weight=.9)], {}
    e = engine(store, risk, AI())
    result = e.tick()
    assert result == ("complete" if kind == "excess_weight" else "failed")
    assert e.broker.submissions == 0


def test_raw_invalid_json_audited_and_classified(store, risk):
    from trading_bot.deepseek_client import DeepSeek
    fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw:
        SimpleNamespace(model="mock", choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content="not json"))]))))
    e = engine(store, risk, DeepSeek("fake", client=fake, audit=store.event))
    assert e.tick() == "failed"
    raw = json.loads(store.db.execute("SELECT payload FROM events WHERE kind='ai_raw'").fetchone()[0])
    error = json.loads(store.db.execute("SELECT payload FROM events WHERE kind='error' ORDER BY id DESC").fetchone()[0])
    assert raw["content"] == "not json" and error["stage"] == "deepseek_schema"


def test_credentials_redacted_in_events_requests_errors(store, monkeypatch, caplog):
    monkeypatch.setenv("ALPACA_API_SECRET", "test-private-secret-999")
    store.error("network", RuntimeError("Authorization: Bearer test-private-secret-999"), ticker="TEST", cycle="cycle")
    store.event("ai_raw", {"api_key": "another-secret", "nested": {"value": "test-private-secret-999"}})
    text = str(store.db.execute("SELECT payload FROM events").fetchall()) + caplog.text
    assert "test-private-secret-999" not in text and "another-secret" not in text
    assert "[REDACTED]" in text


def test_schema_migration_preserves_history(tmp_path):
    import sqlite3
    path = tmp_path / "old.sqlite"
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE events(id INTEGER PRIMARY KEY,ts TEXT,kind TEXT,payload TEXT); INSERT INTO events VALUES(1,'old','historical','{}');")
    db.close()
    store = Store(path)
    assert store.db.execute("SELECT ts FROM events WHERE id=1").fetchone()[0] == "old"
    store.db.close()


def test_orphan_open_order_import_and_pause(store):
    b = FakeBroker()
    b.orders["aip-orphan"] = {"client_order_id": "aip-orphan", "status": "new", "qty": "1", "symbol": "TEST", "side": "buy"}
    reconcile(store, b)
    assert store.pending() == ["aip-orphan"] and store.paused()


def test_read_retries_bounded_no_ssl_change(monkeypatch):
    import requests
    from trading_bot.network import read_retry
    monkeypatch.setattr("trading_bot.network.time.sleep", lambda seconds: None)
    attempts, events = [], []
    def offline():
        attempts.append(1)
        raise requests.Timeout("read timeout")
    with pytest.raises(requests.Timeout) as failure:
        read_retry(offline, lambda *args: events.append(args))
    assert len(attempts) == 3 and len(events) == 2 and failure.value._bot_retries == 2


def test_offline_end_to_end():
    from trading_bot.offline_validation import run_validation
    result = run_validation()
    assert result["simulated_posts"] == 2
    assert result["real_or_paper_orders_submitted"] == 0
    assert result["unresolved"] == []


def test_reserved_sell_already_targets_reduction_not_doubled(risk):
    a = FakeBroker().snapshot()
    a.update(positions={"TEST": 20000}, quantities={"TEST": 200},
             open_orders=[{"symbol": "TEST", "side": "sell", "qty": "30", "filled_qty": "0", "status": "new"}])
    result = risk_plan(risk, decision("SELL", .15), a)
    assert result["quantity"] == 20  # 30 working + 20 new = requested reduction of 50


@pytest.mark.parametrize("problem", ["pause", "blocked", "negative_position", "malformed_reservation"])
def test_sell_still_subject_to_basic_guards(risk, problem):
    a = FakeBroker().snapshot()
    a.update(positions={"TEST": 20000}, quantities={"TEST": 200})
    if problem == "blocked":
        a["blocked"] = True
    if problem == "negative_position":
        a["quantities"]["TEST"] = -1
    if problem == "malformed_reservation":
        a["open_orders"] = [{"symbol": "TEST", "side": "sell", "status": "new"}]
    with pytest.raises(ValueError):
        plan(decision("SELL", .15), a, {"bid": 100, "ask": 100}, {"close": 100},
             fixture_policy(), risk, ["TEST"], 100, problem == "pause")


def test_daily_missing_internal_session_not_hidden_by_extra_row():
    from trading_bot.market_data import daily_history
    df = daily_bars()
    df = df.drop(df.index[10])
    df.loc[pd.Timestamp("2026-05-09", tz="America/New_York")] = [100, 101, 99, 100, 10]
    with pytest.raises(ValueError, match="missing_count=1"):
        daily_history(df, NOW)


def test_daily_bad_ohlc_rejected():
    from trading_bot.market_data import daily_history
    df = daily_bars()
    df.loc[df.index[-1], "high"] = 50
    with pytest.raises(ValueError, match="OHLC range"):
        daily_history(df, NOW)


def test_dashboard_resume_cannot_clear_safety(tmp_path, monkeypatch):
    from test_dashboard import client
    from trading_bot.dashboard import ORIGIN
    monkeypatch.setattr("trading_bot.settings.ioc_permission_confirmed", lambda: True)
    c = client(tmp_path, monkeypatch)
    store = Store(tmp_path/"web.sqlite")
    store.block("IOC rejected")
    store.db.close()
    status = c.get("/api/status", base_url=ORIGIN).json
    headers = {"Origin": ORIGIN, "X-Control-Token": status["control_token"]}
    assert c.post("/api/control/resume", base_url=ORIGIN, headers=headers).status_code == 409
    assert c.get("/api/status", base_url=ORIGIN).json["safety_reason"] == "IOC rejected"


def test_dashboard_unverified_ioc_resume_blocked(tmp_path, monkeypatch):
    from test_dashboard import client
    from trading_bot.dashboard import ORIGIN
    monkeypatch.setattr("trading_bot.settings.ioc_permission_confirmed", lambda: False)
    c = client(tmp_path, monkeypatch)
    status = c.get("/api/status", base_url=ORIGIN).json
    headers = {"Origin": ORIGIN, "X-Control-Token": status["control_token"]}
    assert c.post("/api/control/resume", base_url=ORIGIN, headers=headers).status_code == 409


def test_acknowledgement_never_resumes(store):
    store.block("check broker")
    store.acknowledge()
    assert store.paused() and not store.safety_reason()
    store.reserve("cid", "day", INTENT)
    with pytest.raises(ValueError):
        store.acknowledge()


def test_broker_active_local_terminal_mismatch_blocks(store):
    b = FakeBroker()
    store.reserve("aip-test", "day", INTENT)
    store.order("aip-test", {"status": "canceled"})
    b.orders["aip-test"] = {"status": "new", "client_order_id": "aip-test"}
    reconcile(store, b)
    assert store.pending() == ["aip-test"] and store.paused()
