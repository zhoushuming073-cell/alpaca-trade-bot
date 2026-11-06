import json
import shutil
import sqlite3

import pytest

from trading_bot.backup import backup_database
from trading_bot.recovery import collect_pages, rebuild, validate_facts
from trading_bot.storage import Store


def facts():
    orders = [{"id": "o1", "client_order_id": "manual-1", "symbol": "IREN", "side": "buy",
               "status": "filled", "qty": "100", "filled_qty": "100"}]
    activity = [{"id": "fund", "activity_type": "JNLC", "net_amount": "10000"},
                {"id": "a1", "order_id": "o1", "activity_type": "FILL", "symbol": "IREN", "side": "buy", "qty": "77", "price": "20"},
                {"id": "a2", "order_id": "o1", "activity_type": "FILL", "symbol": "IREN", "side": "buy", "qty": "23", "price": "20"}]
    return orders, activity, {"quantities": {"IREN": 100}, "cash": 8000, "open_orders": [], "blocked": False}


def test_restore_drill_preserves_wal_chat_and_research(tmp_path):
    s = Store(tmp_path / "live.sqlite")
    s.event("test", {"committed_in_wal": True})
    with s._tx():
        s.db.execute("CREATE TABLE assistant_messages(id INTEGER PRIMARY KEY, body TEXT)")
        s.db.execute("INSERT INTO assistant_messages VALUES(1,'retained chat')")
    report = backup_database(s.path, tmp_path / "backups")
    restored = tmp_path / "restored.sqlite"
    shutil.copyfile(report["path"], restored)
    with sqlite3.connect(restored) as c:
        assert c.execute("pragma integrity_check").fetchone()[0] == "ok"
        assert c.execute("select body from assistant_messages").fetchone()[0] == "retained chat"
        assert c.execute("select count(*) from events").fetchone()[0] == 1
    assert report["counts"]["research_profiles"] == 0
    s.close()


def test_backup_rejects_regression_without_destroying_last_good(tmp_path):
    s = Store(tmp_path / "live.sqlite")
    s.event("test", {})
    directory = tmp_path / "backups"
    report = backup_database(s.path, directory)
    with s._tx():
        s.db.execute("DELETE FROM events")
    with pytest.raises(ValueError, match="regressed"):
        backup_database(s.path, directory)
    assert json.loads((directory / "latest.json").read_text(encoding="utf-8")) == report
    assert len(list(directory.glob("*.sqlite"))) == 1
    s.close()


def test_missing_database_is_not_silently_created(tmp_path):
    with pytest.raises(FileNotFoundError):
        backup_database(tmp_path / "missing.sqlite", tmp_path / "backup")
    assert not (tmp_path / "missing.sqlite").exists()


def test_backup_inside_checkout_is_rejected(tmp_path, monkeypatch):
    import trading_bot.backup as backup
    monkeypatch.setattr(backup, "ROOT", tmp_path)
    with pytest.raises(ValueError, match="outside"):
        backup_database(tmp_path / "live.sqlite", tmp_path / "data")


def test_runtime_missing_with_legacy_data_fails_closed(tmp_path, monkeypatch):
    from trading_bot import settings
    repo = tmp_path / "checkout"
    (repo / "data").mkdir(parents=True)
    (repo / "data/paper.sqlite").touch()
    monkeypatch.setattr(settings, "ROOT", repo)
    with pytest.raises(RuntimeError, match="refusing"):
        settings.paper_db_path()
    external = tmp_path / "checkout-runtime"
    external.mkdir(); (external / "paper.sqlite").touch()
    assert settings.paper_db_path() == external / "paper.sqlite"


def test_rebuild_restores_orders_and_partial_fill_facts_idempotently(tmp_path):
    s = Store(tmp_path / "test.sqlite")
    s.pause(True)
    for _ in range(2):
        result = rebuild(s, *facts())
    assert result["fills"] == 2
    assert s.db.execute("select count(*) from orders").fetchone()[0] == 1
    assert s.db.execute("select count(*) from broker_activities").fetchone()[0] == 3
    assert s.order_row("manual-1")["state"] == "FILLED"
    assert s.paused() and s.get_control("autopilot") == "false"
    assert s.freeze_reason() == "DATABASE_RESTORED_RECONCILIATION_REQUIRED"
    s.close()


@pytest.mark.parametrize("problem", ["positions", "cash", "missing_fill", "unknown_order", "open_order"])
def test_bad_broker_facts_block_recovery(problem):
    o, a, account = facts()
    if problem == "positions": account["quantities"]["IREN"] = 99
    if problem == "cash": account["cash"] = 9000
    if problem == "missing_fill": a.pop()
    if problem == "unknown_order": o[0]["status"] = "unknown"
    if problem == "open_order": account["open_orders"] = [o[0]]
    with pytest.raises(ValueError): validate_facts(o, a, account)


def test_pagination_never_accepts_repeated_or_truncated_pages():
    calls = []
    def get(path, params):
        calls.append(dict(params))
        return [{"id": "a"}, {"id": "b"}] if len(calls) == 1 else [{"id": "c"}]
    assert len(collect_pages(get, "/orders", {}, size=2, cursor_name="after_order_id")) == 3
    assert calls[1]["after_order_id"] == "b"
    with pytest.raises(ValueError, match="repeated"):
        collect_pages(lambda *a: [{"id": "a"}], "/orders", {}, size=1, cursor_name="after_order_id")
