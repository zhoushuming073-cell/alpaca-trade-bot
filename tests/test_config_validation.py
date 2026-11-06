import shutil
from datetime import datetime, timezone

import pytest

from trading_bot.decision_schema import Trigger, TriggerPurpose, Condition
from trading_bot.settings import ConfigError, load_config
from trading_bot.market_data import session_info
from test_session_engine import make_engine, plan_with


def test_patrol_and_active_research_have_one_owner():
    bundle = load_config()
    assert not hasattr(bundle.execution, "patrol_interval_seconds")
    assert not hasattr(bundle.execution, "active_research_max_tokens")
    assert bundle.multi_agent.patrol.interval_seconds == 600
    assert bundle.multi_agent.active_research.min_cooldown_seconds == 120


def test_invalid_execution_field_reports_exact_file_and_field(tmp_path):
    root = tmp_path
    shutil.copytree("config", root / "config")
    with (root / "config" / "execution.yaml").open("a", encoding="utf-8") as handle:
        handle.write("\npatrol_interval_seconds: 600\n")
    with pytest.raises(ConfigError) as failure:
        load_config(root)
    assert failure.value.file == "config/execution.yaml"
    assert "patrol_interval_seconds" in failure.value.field


def test_invalid_multi_agent_field_reports_exact_file(tmp_path):
    root = tmp_path
    shutil.copytree("config", root / "config")
    with (root / "config" / "multi_agent.yaml").open("a", encoding="utf-8") as handle:
        handle.write("\nunknown_patrol_field: true\n")
    with pytest.raises(ConfigError) as failure:
        load_config(root)
    assert failure.value.file == "config/multi_agent.yaml"
    assert "unknown_patrol_field" in failure.value.field


def test_1107_active_plan_is_intraday_not_premarket(store, clock, bundle):
    now = datetime(2026, 11, 5, 16, 7, 10, tzinfo=timezone.utc)  # 11:07 ET
    session = session_info(now, bundle.execution)
    assert session.active and not session.premarket
    engine, _, _ = make_engine(store, clock, bundle)
    plan = plan_with(now, trigger=Trigger(
        trigger_id="exec", purpose=TriggerPurpose.EXECUTE_ORDER,
        all_of=[Condition(field="last", op="<=", value=101)],
    ))
    saved, blocked = engine._save_plans(session, plan, ["CRWV"], {}, agent_run_id="", agent_report_id="")
    assert saved == 1 and blocked == 0
    row = store.active_plans("2026-11-05")[0]
    assert row["payload"]["mode"] == "INTRADAY_PLAN"


def test_invalid_hot_config_blocks_and_records_file_field(store, clock, bundle, monkeypatch):
    from trading_bot.engine import Engine
    from test_session_engine import make_engine
    engine, _, _ = make_engine(store, clock, bundle)
    def bad():
        raise ConfigError("config/execution.yaml", "patrol_model", "extra_forbidden")
    monkeypatch.setattr("trading_bot.engine.load_config", bad)
    assert engine._config_pending() is True
    assert store.config_pending() is True
    event = store.recent_events(("config_load",), 1)[0]["payload"]
    assert event["file"] == "config/execution.yaml"
    assert event["field"] == "patrol_model"
    assert event["reason"] == "extra_forbidden"
