from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from trading_bot.agent_schemas import CritiqueReport, ResearchReport, ResearchScan
from trading_bot.decision_schema import RecheckResponse

NOW = datetime.now(timezone.utc)


def report(**overrides):
    value = {
        "run_id": "r1", "as_of_utc": NOW.isoformat(), "universe_version": "u1",
        "symbol": "CRWV", "setup_type": "PULLBACK_RECLAIM", "opportunity_grade": "A",
        "technical_structure": "support", "fundamental_context": "fixture",
        "event_evidence": ["e1"], "supporting_evidence_ids": ["e1"],
        "counterevidence_ids": [], "entry_conditions": ["last <= 101"],
        "invalidation_conditions": ["last < 95"], "potential_upside": "fixture",
        "downside_scenarios": ["fixture"], "data_quality_flags": [], "unknowns": [],
        "research_conclusion": "FAVORABLE",
    }
    value.update(overrides)
    return value


def test_research_report_is_strict_and_has_no_order_fields():
    parsed = ResearchReport.model_validate(report())
    assert parsed.research_conclusion == "FAVORABLE"
    with pytest.raises(ValidationError):
        ResearchReport.model_validate(report(order_intent={"action": "BUY"}))
    with pytest.raises(ValidationError):
        ResearchReport.model_validate(report(extra_field="nope"))


def test_research_scan_rejects_extra_fields_and_bad_ids():
    scan = ResearchScan.model_validate({
        "run_id": "r1", "as_of_utc": NOW.isoformat(), "universe_version": "u1",
        "candidates": [{"symbol": "CRWV", "setup_type": "EVENT_MISPRICING",
                        "opportunity_grade": "B", "thesis": "fixture",
                        "evidence_ids": [1], "priority": 2}],
        "skipped_symbols": [], "notes": "",
    })
    assert scan.candidates[0].evidence_ids == ["1"]
    with pytest.raises(ValidationError):
        ResearchScan.model_validate({
            "run_id": "r1", "as_of_utc": NOW.isoformat(), "universe_version": "u1",
            "candidates": [], "skipped_symbols": [], "notes": "", "unexpected": True,
        })


def test_critique_requires_assessment_and_no_order_fields():
    with pytest.raises(ValidationError):
        CritiqueReport.model_validate({
            "run_id": "r1", "research_report_id": "rr1", "as_of_utc": NOW.isoformat(),
            "symbol": "CRWV", "critique_severity": "HIGH",
            "overall_assessment": "OPPOSE", "order_intent": {},
        })


def test_recheck_metadata_is_optional_and_strict():
    value = RecheckResponse.model_validate({
        "as_of_utc": NOW.isoformat(), "plan_id": "p1", "decision": "CONTINUE",
        "reason": "fixture", "order_intent": None,
        "run_id": "r1", "research_report_ids": ["rr1"], "critique_report_ids": ["cr1"],
    })
    assert value.run_id == "r1"
    assert value.research_report_ids == ["rr1"]
    with pytest.raises(ValidationError):
        RecheckResponse.model_validate({
            "as_of_utc": NOW.isoformat(), "plan_id": "p1", "decision": "CONTINUE",
            "reason": "fixture", "order_intent": None, "order": {},
        })
