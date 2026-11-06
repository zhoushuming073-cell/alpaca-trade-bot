from datetime import datetime, timedelta, timezone

import pytest

from trading_bot.agent_schemas import CritiqueReport, ResearchReport, ResearchScan
from trading_bot.decision_schema import MarketDecision, PremarketPlan
from trading_bot.dry_run import FakeBroker, _days, _minutes
from trading_bot.research_only import ReadOnlyBroker, ResearchOnlyError, run_research_only
from trading_bot.settings import load_config
from trading_bot.storage import Store

NOW = datetime(2026, 11, 5, 14, 5, 10, tzinfo=timezone.utc)


class ShadowAI:
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
                candidates=[{"symbol": "CRWV", "setup_type": "PULLBACK_RECLAIM",
                             "opportunity_grade": "A", "thesis": "fixture",
                             "evidence_ids": ["e1"], "priority": 1}],
            ).model_dump_json()
        elif mode == "RESEARCH_REPORT":
            raw = ResearchReport(
                run_id=payload["run_id"], as_of_utc=as_of,
                universe_version=payload["universe_version"], symbol=payload["symbol"],
                setup_type="PULLBACK_RECLAIM", opportunity_grade="A",
                technical_structure="fixture", fundamental_context="fixture",
                event_evidence=["fixture"], supporting_evidence_ids=["e1"],
                counterevidence_ids=[], entry_conditions=["last <= 101"],
                invalidation_conditions=["last < 95"], potential_upside="fixture",
                downside_scenarios=["fixture"], data_quality_flags=[], unknowns=[],
                research_conclusion="FAVORABLE",
            ).model_dump_json()
        elif mode == "SKEPTIC_REVIEW":
            raw = CritiqueReport(
                run_id=payload["run_id"], research_report_id=payload["research_report_id"],
                as_of_utc=as_of, symbol=payload["symbol"], verified_claims=["fixture"],
                unsupported_claims=[], contradictory_evidence=[], alternative_explanations=[],
                data_gaps=[], major_risks=[], thesis_failure_scenarios=[],
                critique_severity="LOW", overall_assessment="SUPPORT", evidence_ids=["e1"],
            ).model_dump_json()
        elif mode in {"PORTFOLIO_MANAGER", "PREMARKET_PLAN"}:
            candidates = []
            if kwargs.get("role") != "baseline":
                report = payload["research_reports"][0]
                critique = payload["critique_reports"][0]
                candidates = [{
                    "symbol": "CRWV", "setup_type": "PULLBACK_RECLAIM",
                    "opportunity_grade": "A", "trade_decision": "OPEN_SHORT",
                    "analysis": "fixture shadow", "evidence_ids": ["e1"],
                    "plan_id": "shadow-plan",
                    "plan_valid_until_utc": (as_of + timedelta(minutes=20)).isoformat(),
                    "triggers": [{"trigger_id": "exec", "purpose": "EXECUTE_ORDER",
                                  "all_of": [{"field": "last", "op": "<=", "value": 101}]}],
                    "order_intent": {
                        "action": "OPEN_SHORT", "symbol": "CRWV", "qty": 1,
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
                plan_id="shadow-plan", candidates=candidates,
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


def test_read_only_proxy_blocks_writes():
    proxy = ReadOnlyBroker(FakeBroker())
    with pytest.raises(ResearchOnlyError):
        proxy.dispatch({}, "cid")
    with pytest.raises(ResearchOnlyError):
        proxy.cancel("cid")


def test_paper_client_final_request_guard_blocks_writes_without_network():
    from trading_bot.broker import build_broker

    broker = build_broker(
        {"ALPACA_API_KEY": "fake", "ALPACA_API_SECRET": "fake"},
        start_orders_thread=False, read_only=True,
    )
    with pytest.raises(RuntimeError, match="RESEARCH_ONLY"):
        broker.api._request("POST", "/orders", {"symbol": "AAPL"})


def test_research_only_persists_reports_and_zero_plans_or_orders(tmp_path):
    bundle = load_config()
    store = Store(tmp_path / "shadow.sqlite", instance_id="shadow", clock=lambda: NOW)
    try:
        store.pause(True)
        result = run_research_only(
            bundle=bundle, store=store, broker=FakeBroker(), ai=ShadowAI(),
            data=lambda symbol: _minutes(NOW), daily_data=lambda symbol: _days(NOW),
            clock=lambda: NOW,
        )
        assert store.paused() is True
        assert result["mode"] == "RESEARCH_ONLY"
        assert result["in_premarket_window"] is True
        assert result["status"] == "COMPLETE"
        assert result["orders_written"] == 0
        assert result["plans_written"] == 0
        assert result["shadow_plan"]["candidates"][0]["trade_decision"] == "OPEN_SHORT"
        assert result["baseline"] is not None
        assert store.agent_reports(run_id=result["run_id"])
        assert store.agent_run(result["run_id"])["mode"] == "RESEARCH_ONLY"
        assert store.active_plans("2026-11-05") == []
        assert store.orders_for_day("2026-11-05") == []
    finally:
        store.close()
