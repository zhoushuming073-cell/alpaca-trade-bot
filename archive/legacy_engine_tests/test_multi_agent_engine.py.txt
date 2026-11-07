from dataclasses import replace
from datetime import datetime, timedelta, timezone

from trading_bot.agent_schemas import CritiqueReport, ResearchReport, ResearchScan
from trading_bot.decision_schema import (
    MarketDecision, PremarketPlan,
)
from trading_bot.dry_run import FakeBroker, _days, _minutes
from trading_bot.engine import Engine
from trading_bot.settings import load_config

NOW = datetime(2026, 11, 5, 14, 5, 10, tzinfo=timezone.utc)


class AgentAI:
    model = "fake"

    def __init__(self):
        self.calls = []

    def complete(self, mode, payload, parser, **kwargs):
        self.calls.append((kwargs.get("role"), mode))
        as_of = datetime.fromisoformat(str(payload["as_of_utc"]).replace("Z", "+00:00"))
        if mode == "RESEARCHER_SCAN":
            raw = ResearchScan(
                run_id=payload["run_id"], as_of_utc=as_of,
                universe_version=payload["universe_version"],
                candidates=[{
                    "symbol": "CRWV", "setup_type": "PULLBACK_RECLAIM",
                    "opportunity_grade": "A", "thesis": "fixture",
                    "evidence_ids": ["e1"], "priority": 1,
                }],
            ).model_dump_json()
        elif mode == "RESEARCH_REPORT":
            raw = ResearchReport(
                run_id=payload["run_id"], as_of_utc=as_of,
                universe_version=payload["universe_version"],
                symbol=payload["symbol"], setup_type="PULLBACK_RECLAIM",
                opportunity_grade="A", technical_structure="fixture support",
                fundamental_context="fixture", event_evidence=["fixture"],
                supporting_evidence_ids=["e1"], counterevidence_ids=[],
                entry_conditions=["last <= 101"], invalidation_conditions=["last < 95"],
                potential_upside="fixture", downside_scenarios=["fixture"],
                data_quality_flags=[], unknowns=[], research_conclusion="FAVORABLE",
            ).model_dump_json()
        elif mode == "SKEPTIC_REVIEW":
            raw = CritiqueReport(
                run_id=payload["run_id"], research_report_id=payload["research_report_id"],
                as_of_utc=as_of, symbol=payload["symbol"], verified_claims=["fixture"],
                unsupported_claims=[], contradictory_evidence=[], alternative_explanations=[],
                data_gaps=[], major_risks=[], thesis_failure_scenarios=["last < 95"],
                critique_severity="LOW", overall_assessment="SUPPORT", evidence_ids=["e1"],
            ).model_dump_json()
        elif mode in {"PORTFOLIO_MANAGER", "PREMARKET_PLAN"}:
            candidates = []
            if kwargs.get("role") != "baseline":
                report = payload["research_reports"][0]
                critique = payload["critique_reports"][0]
                candidates = [{
                    "symbol": "CRWV", "setup_type": "PULLBACK_RECLAIM",
                    "opportunity_grade": "A", "trade_decision": "OPEN_LONG",
                    "analysis": "fixture", "evidence_ids": ["e1"],
                    "plan_id": "plan-fixture",
                    "plan_valid_until_utc": (as_of + timedelta(minutes=40)).isoformat(),
                    "triggers": [{"trigger_id": "exec", "purpose": "EXECUTE_ORDER",
                                  "all_of": [{"field": "last", "op": "<=", "value": 101}]}],
                    "order_intent": {
                        "action": "OPEN_LONG", "symbol": "CRWV", "qty": 2,
                        "limit_price": "100", "order_type": "limit",
                        "time_in_force": "day", "extended_hours": False,
                        "stop_policy": "NONE", "stop_loss": None,
                    },
                    "research_report_id": report["report_id"],
                    "critique_report_id": critique["critique_id"],
                    "counterevidence": ["fixture"],
                }]
            plan = PremarketPlan(
                as_of_utc=as_of, model_id="fake",
                market_decision=MarketDecision.TRADE if candidates else MarketDecision.NO_TRADE,
                account_snapshot_id=payload["account"]["account_snapshot_id"],
                plan_id="plan-fixture", candidates=candidates,
                run_id=payload["run_id"] if kwargs.get("role") != "baseline" else "",
                decision_mode="THREE_AGENT" if kwargs.get("role") != "baseline" else "SINGLE_AGENT",
                research_report_ids=[report["report_id"]] if candidates else [],
                critique_report_ids=[critique["critique_id"]] if candidates else [],
            )
            raw = plan.model_dump_json()
        else:
            raise AssertionError(mode)
        return parser(raw), {
            "mode": mode, "model": "fake", "prompt_tokens": 1, "completion_tokens": 1,
            "latency_ms": 1, "raw": raw, "prompt_hash": "p", "input_hash": "i", "output_hash": "o",
        }


def test_engine_multi_agent_saves_plan_without_submitting(store, clock, monkeypatch):
    bundle = load_config()
    bundle = replace(
        bundle,
        multi_agent=bundle.multi_agent.model_copy(update={"enabled": True}),
        hashes={**bundle.hashes, "combined": "agent-engine-fixture"},
    )
    monkeypatch.setattr("trading_bot.settings.load_config", lambda: bundle)
    clock.set(NOW)
    broker = FakeBroker()
    engine = Engine(
        store, broker, AgentAI(), bundle,
        data=lambda symbol: _minutes(clock()),
        daily_data=lambda symbol: _days(clock()),
        clock=clock, mode="dry-run", require_watchdog=False, check_disk_config=False,
    )
    assert engine.tick() == "premarket_planned"
    plans = store.active_plans("2026-11-05")
    assert len(plans) == 1
    plan = plans[0]
    assert plan["payload"]["run_id"].startswith("ma-")
    assert plan["payload"]["decision_mode"] == "THREE_AGENT"
    candidate = plan["payload"]["candidate"]
    assert candidate["research_report_id"].startswith("rr-")
    assert candidate["critique_report_id"].startswith("cr-")
    run_id = plan["payload"]["run_id"]
    assert store.agent_run(run_id)["status"] == "COMPLETE"
    assert store.agent_plan_links(run_id)

    clock.set(datetime(2026, 11, 5, 14, 35, 10, tzinfo=timezone.utc))
    assert engine.tick() == "active_triggered"
    assert broker.submissions == 1, store.recent_events(("intent_rejected", "order_not_sent", "error", "plan_invalidated"), 20)
    assert broker.positions.get("CRWV") == 2

    # Next trading day: reconcile the fake fill first, then a new bounded
    # three-agent run must read the previous reports from SQLite memory.
    clock.set(datetime(2026, 11, 6, 14, 5, 10, tzinfo=timezone.utc))
    assert engine.tick() == "premarket_planned"
    assert len(store.recent_agent_runs(10)) >= 2
    assert store.agent_memory(["CRWV"])
    assert store.agent_experiments(10)
