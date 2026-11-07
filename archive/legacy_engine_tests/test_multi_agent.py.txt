from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

import pytest

from trading_bot.agent_schemas import CritiqueReport, ResearchReport, ResearchScan
from trading_bot.decision_schema import (
    Candidate, Condition, MarketDecision, OpportunityGrade, OrderIntent,
    PremarketPlan, RecheckDecision, RecheckResponse, SetupType, TradeDecision,
    Trigger, TriggerPurpose,
)
from trading_bot.multi_agent import MultiAgentOrchestrator
from trading_bot.settings import load_config
from trading_bot.storage import Store

NOW = datetime.now(timezone.utc)


class FakeAI:
    def __init__(self, *, candidates=None, pm="TRADE", fail=None, invalid_scan=False,
                 critique_assessment="SUPPORT", pm_bad=None):
        self.candidates = candidates if candidates is not None else ["CRWV"]
        self.pm = pm
        self.fail = fail
        self.invalid_scan = invalid_scan
        self.critique_assessment = critique_assessment
        self.pm_bad = pm_bad
        self.calls = []

    def complete(self, mode, payload, parser, **kwargs):
        role = kwargs.get("role")
        self.calls.append((role, mode))
        if self.fail == role:
            raise TimeoutError("fake timeout")
        if mode == "RESEARCHER_SCAN":
            if self.invalid_scan:
                return parser("{not-json"), self._meta(mode)
            scan = ResearchScan(
                run_id=payload["run_id"], as_of_utc=NOW, universe_version=payload["universe_version"],
                candidates=[
                    {
                        "symbol": symbol, "setup_type": "PULLBACK_RECLAIM",
                        "opportunity_grade": "A", "thesis": "fixture research",
                        "evidence_ids": ["e1"], "priority": 1,
                    } for symbol in self.candidates
                ],
            )
        elif mode == "RESEARCH_REPORT":
            symbol = payload["symbol"]
            report = ResearchReport(
                run_id=payload["run_id"], as_of_utc=NOW,
                universe_version=payload["universe_version"], symbol=symbol,
                setup_type="PULLBACK_RECLAIM", opportunity_grade="A",
                technical_structure="fixture support and reclaim",
                fundamental_context="fixture fundamentals",
                event_evidence=["fixture evidence"],
                supporting_evidence_ids=["e1"], counterevidence_ids=[],
                entry_conditions=["last <= 101"], invalidation_conditions=["last < 95"],
                potential_upside="fixture scenario", downside_scenarios=["fixture downside"],
                data_quality_flags=[], unknowns=[], research_conclusion="FAVORABLE",
            )
        elif mode == "SKEPTIC_REVIEW":
            report_id = payload["research_report_id"]
            critique = CritiqueReport(
                run_id=payload["run_id"], research_report_id=report_id, as_of_utc=NOW,
                symbol=payload["symbol"], verified_claims=["fixture verified"],
                unsupported_claims=[], contradictory_evidence=[], alternative_explanations=[],
                data_gaps=[], major_risks=["fixture risk"], thesis_failure_scenarios=["last < 95"],
                critique_severity="LOW", overall_assessment=self.critique_assessment, evidence_ids=["e1"],
            )
        elif mode == "PORTFOLIO_RECHECK":
            report = payload["research_reports"][0]
            critique = payload["critique_reports"][0]
            recheck = RecheckResponse(
                as_of_utc=datetime.fromisoformat(str(payload["as_of_utc"]).replace("Z", "+00:00")),
                plan_id=payload["plan_id"], decision=RecheckDecision.CONTINUE,
                reason="fixture continue", order_intent=None,
                run_id=payload["run_id"],
                research_report_ids=[report["report_id"]],
                critique_report_ids=[critique["critique_id"]],
            )
            raw = recheck.model_dump_json()
            return parser(raw), self._meta(mode)
        elif mode in {"PREMARKET_PLAN", "PORTFOLIO_MANAGER"}:
            reports = {item["symbol"]: item for item in payload.get("research_reports", [])}
            critiques = {item["symbol"]: item for item in payload.get("critique_reports", [])}
            candidates = []
            if role != "baseline" and self.pm == "TRADE" and reports and self.pm_bad != "no_trade":
                symbol, report = next(iter(reports.items()))
                critique = critiques[symbol]
                if self.pm_bad == "outside":
                    symbol = "ZZZZ"
                candidates.append({
                    "symbol": symbol, "setup_type": "PULLBACK_RECLAIM",
                    "opportunity_grade": "A", "trade_decision": "OPEN_LONG",
                    "analysis": "fixture plan", "evidence_ids": ["e1"],
                    "plan_id": "pm-plan", "plan_valid_until_utc": (NOW + timedelta(minutes=20)).isoformat(),
                    "triggers": [{"trigger_id": "exec", "purpose": "EXECUTE_ORDER",
                                  "all_of": [{"field": "last", "op": "<=", "value": 101.0}]}],
                    "order_intent": {
                        "action": "OPEN_LONG", "symbol": symbol, "qty": 2,
                        "limit_price": "100.0", "order_type": "limit",
                        "time_in_force": "day", "extended_hours": False,
                        "stop_policy": "NONE", "stop_loss": None,
                    },
                    "research_report_id": "" if self.pm_bad == "no_ids" else report["report_id"],
                    "critique_report_id": "" if self.pm_bad == "no_ids" else critique["critique_id"],
                    "counterevidence": ["fixture counterevidence"],
                })
            if role == "baseline":
                candidates = []
            plan = PremarketPlan(
                as_of_utc=NOW, model_id="fake",
                market_decision=MarketDecision.TRADE if candidates else MarketDecision.NO_TRADE,
                account_snapshot_id=payload["account"]["account_snapshot_id"],
                plan_id="pm-plan", candidates=candidates,
                run_id=payload["run_id"] if role != "baseline" else "",
                decision_mode="THREE_AGENT" if role != "baseline" else "SINGLE_AGENT",
                research_report_ids=[r["report_id"] for r in reports.values()],
                critique_report_ids=[c["critique_id"] for c in critiques.values()],
            )
        else:
            raise AssertionError(mode)
        return parser(plan.model_dump_json() if mode in {"PREMARKET_PLAN", "PORTFOLIO_MANAGER"} else scan.model_dump_json()
                      if mode == "RESEARCHER_SCAN" else report.model_dump_json()
                      if mode == "RESEARCH_REPORT" else critique.model_dump_json()), self._meta(mode)

    @staticmethod
    def _meta(mode):
        return {
            "mode": mode, "model": "fake", "prompt_tokens": 10, "completion_tokens": 5,
            "latency_ms": 2, "raw": "{}", "prompt_hash": "p", "input_hash": "i",
            "output_hash": "o",
        }


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "agents.sqlite", instance_id="agents")
    yield s
    s.close()


def bundle_with(enabled=True):
    bundle = load_config()
    multi = bundle.multi_agent.model_copy(update={"enabled": enabled})
    return replace(bundle, multi_agent=multi)


def base_payload():
    return {
        "as_of_utc": NOW.isoformat(), "model_id": "fake",
        "account": {"account_snapshot_id": "snap-1", "positions": [], "open_orders": []},
        "account_snapshot_id": "snap-1",
        "strategy": {"version": "fixture", "analysis_rules": "fixture"},
        "research": {}, "news": {},
        "execution_contract": {"mode": "Paper only"},
    }


def focus_loader(symbols):
    return [{
        "symbol": symbol, "held": False, "position": None, "shortability": {},
        "history": {}, "quote": {"last": 100.0},
    } for symbol in symbols]


def run(store, ai, *, enabled=True, deadline=None):
    bundle = bundle_with(enabled)
    orchestrator = MultiAgentOrchestrator(
        store, ai, bundle, clock=lambda: NOW,
        run_id_factory=lambda: "run-test",
    )
    return orchestrator.run_premarket(
        session=None, base_payload=base_payload(),
        universe=[{"symbol": "CRWV", "history": {}, "data_status": "OK"}],
        held={}, focus_loader=focus_loader,
        deadline_utc=deadline or NOW + timedelta(minutes=30),
        universe_version="u1", trading_day="2026-11-06",
    )


def test_three_agents_complete_and_persist_separately(store):
    result = run(store, FakeAI())
    assert result.status == "COMPLETE", result.error
    assert result.plan is not None
    assert result.plan.decision_mode == "THREE_AGENT"
    assert result.plan.candidates[0].order_intent is not None
    assert len(result.reports) == 1
    assert len(result.critiques) == 1
    roles = [row["role"] for row in store.agent_reports(run_id="run-test")]
    assert "researcher" in roles and "skeptic" in roles and "portfolio_manager" in roles
    assert store.agent_run("run-test")["status"] == "COMPLETE"


def test_no_candidates_returns_no_trade(store):
    result = run(store, FakeAI(candidates=[]))
    assert result.status == "NO_TRADE", result.error
    assert result.plan is not None
    assert result.plan.market_decision.value == "NO_TRADE"


def test_skeptic_disagreement_is_recorded_not_voted(store):
    result = run(store, FakeAI(critique_assessment="OPPOSE"))
    assert result.status == "COMPLETE"
    critique = next(row for row in store.agent_reports(run_id="run-test") if row["role"] == "skeptic")
    assert critique["parsed"]["overall_assessment"] == "OPPOSE"
    assert result.plan is not None
    assert result.plan.candidates[0].order_intent is not None


def test_pm_outside_pool_is_rejected(store):
    result = run(store, FakeAI(pm_bad="outside"))
    assert result.status == "FAILED"
    assert result.plan is None


def test_pm_order_without_research_and_critique_is_rejected(store):
    result = run(store, FakeAI(pm_bad="no_ids"))
    assert result.status == "FAILED"
    assert result.plan is None


def test_pm_no_trade_is_valid(store):
    result = run(store, FakeAI(pm_bad="no_trade"))
    assert result.status == "NO_TRADE"
    assert result.plan is not None
    assert result.plan.market_decision.value == "NO_TRADE"


def test_baseline_is_shadow_only(store):
    result = run(store, FakeAI())
    assert result.baseline is not None
    baseline = next(row for row in store.agent_reports(run_id="run-test") if row["role"] == "baseline")
    assert baseline["parsed"]["decision_mode"] == "SINGLE_AGENT"
    # Baseline is not linked to any executable plan.
    assert store.agent_plan_links("run-test") == []


def test_budget_is_finite_and_fails_closed(store):
    bundle = bundle_with(True)
    tight = bundle.multi_agent.budget.model_copy(update={"max_session_tokens": 1})
    bundle = replace(
        bundle,
        multi_agent=bundle.multi_agent.model_copy(update={"budget": tight}),
    )
    orchestrator = MultiAgentOrchestrator(store, FakeAI(), bundle, clock=lambda: NOW,
                                          run_id_factory=lambda: "run-budget")
    result = orchestrator.run_premarket(
        session=None, base_payload=base_payload(),
        universe=[{"symbol": "CRWV", "history": {}, "data_status": "OK"}],
        held={}, focus_loader=focus_loader, deadline_utc=NOW + timedelta(minutes=30),
        universe_version="u1", trading_day="2026-11-06",
    )
    assert result.status == "FAILED"
    assert result.plan is None


def test_budget_estimate_blocks_before_provider_call(store):
    bundle = bundle_with(True)
    tight = bundle.multi_agent.budget.model_copy(update={"max_session_tokens": 3000})
    bundle = replace(
        bundle,
        multi_agent=bundle.multi_agent.model_copy(update={"budget": tight}),
    )
    ai = FakeAI()
    orchestrator = MultiAgentOrchestrator(store, ai, bundle, clock=lambda: NOW,
                                          run_id_factory=lambda: "run-preflight")
    result = orchestrator.run_premarket(
        session=None, base_payload=base_payload(),
        universe=[{"symbol": "CRWV", "history": {"daily_recent": [{"close": 100}]}, "data_status": "OK"}],
        held={}, focus_loader=focus_loader, deadline_utc=NOW + timedelta(minutes=30),
        universe_version="u1", trading_day="2026-11-06",
    )
    assert result.status == "FAILED"
    assert ai.calls == []


def test_skeptic_timeout_blocks_symbol_and_never_falls_back_to_buy(store):
    result = run(store, FakeAI(fail="skeptic"))
    assert result.status in {"FAILED", "NO_TRADE"}
    assert "CRWV" in result.blocked_symbols
    assert result.plan is None or result.plan.market_decision.value == "NO_TRADE"


def test_pm_failure_returns_no_plan(store):
    result = run(store, FakeAI(fail="portfolio_manager"))
    assert result.status == "FAILED"
    assert result.plan is None


def test_scan_failure_fails_closed(store):
    result = run(store, FakeAI(invalid_scan=True))
    assert result.status == "FAILED"
    assert result.plan is None


def test_deadline_prevents_any_plan(store):
    result = run(store, FakeAI(), deadline=NOW - timedelta(seconds=1))
    assert result.status == "FAILED"
    assert result.plan is None


def test_shorting_is_not_a_default_agent_permission(bundle=None):
    current = load_config()
    assert current.multi_agent.enabled is True
    assert current.multi_agent.comparison.baseline_can_trade is False
    assert current.multi_agent.budget.effective_max_tokens is None
    assert current.multi_agent.budget.effective_max_cost > 0


def test_disabled_mode_does_not_call_ai(store):
    ai = FakeAI()
    with pytest.raises(Exception):
        run(store, ai, enabled=False)
    assert ai.calls == []


def test_recheck_runs_researcher_critic_and_pm(store):
    bundle = bundle_with(True)
    orchestrator = MultiAgentOrchestrator(
        store, FakeAI(), bundle, clock=lambda: NOW,
        run_id_factory=lambda: "run-recheck",
    )
    result = orchestrator.run_recheck(
        session=None, base_payload=base_payload(), symbol="CRWV",
        existing_plan={"plan_id": "p1", "candidate": {"symbol": "CRWV"}},
        focus_row=focus_loader(["CRWV"])[0],
        deadline_utc=NOW + timedelta(minutes=10), trading_day="2026-11-06",
    )
    assert result.status == "CONTINUE", result.error
    assert result.response is not None
    assert result.response.decision.value == "CONTINUE"
    assert {row["role"] for row in store.agent_reports(run_id="run-recheck")} >= {
        "researcher", "skeptic", "portfolio_manager",
    }
