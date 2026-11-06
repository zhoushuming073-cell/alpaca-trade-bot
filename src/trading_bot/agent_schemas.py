"""Versioned schemas for the three in-process research/decision agents.

The researcher and skeptic cannot emit order fields.  The portfolio manager's
only executable output is the existing strict ``PremarketPlan`` schema, which
is validated again by the deterministic engine before any broker call.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import Field, field_validator

from .decision_schema import OpportunityGrade, SetupType
from .settings import Strict


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("UTC timestamp required")
    return value.astimezone(timezone.utc)


class ResearchScanItem(Strict):
    symbol: str = Field(min_length=1, max_length=12)
    setup_type: SetupType
    opportunity_grade: OpportunityGrade
    direction: Literal["LONG", "SHORT", "NEUTRAL", "INSUFFICIENT"] = "NEUTRAL"
    thesis: str = Field(min_length=1, max_length=2500)
    evidence_ids: list[str] = Field(default_factory=list, max_length=30)
    priority: int = Field(default=50, ge=1, le=100)

    @field_validator("evidence_ids", mode="before")
    @classmethod
    def _ids(cls, value):
        if value is None:
            return []
        return [str(item) for item in value if item is not None]


class ResearchScan(Strict):
    schema_version: Literal["1.0"] = "1.0"
    mode: Literal["RESEARCHER_SCAN"] = "RESEARCHER_SCAN"
    run_id: str = Field(min_length=1, max_length=120)
    as_of_utc: datetime
    universe_version: str = Field(min_length=1, max_length=120)
    candidates: list[ResearchScanItem] = Field(default_factory=list, max_length=24)
    skipped_symbols: list[str] = Field(default_factory=list, max_length=60)
    notes: str = Field(default="", max_length=2000)

    @field_validator("as_of_utc")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return _utc(value)


class ResearchReport(Strict):
    schema_version: Literal["1.0"] = "1.0"
    mode: Literal["RESEARCH_REPORT"] = "RESEARCH_REPORT"
    run_id: str = Field(min_length=1, max_length=120)
    research_report_id: str = Field(default="", max_length=140)
    as_of_utc: datetime
    universe_version: str = Field(min_length=1, max_length=120)
    symbol: str = Field(min_length=1, max_length=12)
    setup_type: SetupType
    opportunity_grade: OpportunityGrade
    direction: Literal["LONG", "SHORT", "NEUTRAL", "INSUFFICIENT"] = "NEUTRAL"
    technical_structure: str = Field(min_length=1, max_length=4000)
    fundamental_context: str = Field(min_length=1, max_length=4000)
    event_evidence: list[str] = Field(default_factory=list, max_length=30)
    supporting_evidence_ids: list[str] = Field(default_factory=list, max_length=50)
    counterevidence_ids: list[str] = Field(default_factory=list, max_length=50)
    entry_conditions: list[str] = Field(min_length=1, max_length=20)
    invalidation_conditions: list[str] = Field(min_length=1, max_length=20)
    potential_upside: str = Field(min_length=1, max_length=2000)
    downside_scenarios: list[str] = Field(min_length=1, max_length=20)
    data_quality_flags: list[str] = Field(default_factory=list, max_length=30)
    unknowns: list[str] = Field(default_factory=list, max_length=30)
    research_conclusion: Literal["FAVORABLE", "WATCH", "UNFAVORABLE", "INSUFFICIENT"]

    @field_validator("as_of_utc")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return _utc(value)

    @field_validator("supporting_evidence_ids", "counterevidence_ids", mode="before")
    @classmethod
    def _ids(cls, value):
        if value is None:
            return []
        return [str(item) for item in value if item is not None]


class CritiqueReport(Strict):
    schema_version: Literal["1.0"] = "1.0"
    mode: Literal["SKEPTIC_REVIEW"] = "SKEPTIC_REVIEW"
    run_id: str = Field(min_length=1, max_length=120)
    critique_id: str = Field(default="", max_length=140)
    research_report_id: str = Field(min_length=1, max_length=140)
    as_of_utc: datetime
    symbol: str = Field(min_length=1, max_length=12)
    direction_reviewed: Literal["LONG", "SHORT", "NEUTRAL", "INSUFFICIENT"] = "NEUTRAL"
    verified_claims: list[str] = Field(default_factory=list, max_length=30)
    unsupported_claims: list[str] = Field(default_factory=list, max_length=30)
    contradictory_evidence: list[str] = Field(default_factory=list, max_length=30)
    alternative_explanations: list[str] = Field(default_factory=list, max_length=20)
    data_gaps: list[str] = Field(default_factory=list, max_length=30)
    major_risks: list[str] = Field(default_factory=list, max_length=30)
    squeeze_risks: list[str] = Field(default_factory=list, max_length=20)
    thesis_failure_scenarios: list[str] = Field(default_factory=list, max_length=30)
    critique_severity: Literal["NONE", "LOW", "MEDIUM", "HIGH"]
    overall_assessment: Literal["SUPPORT", "QUALIFIED", "OPPOSE", "INSUFFICIENT"]
    evidence_ids: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("as_of_utc")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return _utc(value)

    @field_validator("evidence_ids", mode="before")
    @classmethod
    def _ids(cls, value):
        if value is None:
            return []
        return [str(item) for item in value if item is not None]


def parse_research_scan(raw: str, *, run_id: str | None = None, now: datetime | None = None) -> ResearchScan:
    scan = ResearchScan.model_validate_json(raw)
    if run_id is not None and scan.run_id != run_id:
        raise ValueError("Researcher run_id mismatch")
    if now is not None:
        if abs((scan.as_of_utc - now).total_seconds()) > 900:
            raise ValueError("Researcher scan is stale")
    return scan


def parse_research_report(raw: str, *, run_id: str | None = None, symbol: str | None = None,
                           now: datetime | None = None) -> ResearchReport:
    report = ResearchReport.model_validate_json(raw)
    if run_id is not None and report.run_id != run_id:
        raise ValueError("Research report run_id mismatch")
    if symbol is not None and report.symbol != symbol:
        raise ValueError("Research report symbol mismatch")
    if now is not None and abs((report.as_of_utc - now).total_seconds()) > 900:
        raise ValueError("Research report is stale")
    return report


def parse_critique(raw: str, *, run_id: str | None = None, symbol: str | None = None,
                   research_report_id: str | None = None, now: datetime | None = None) -> CritiqueReport:
    critique = CritiqueReport.model_validate_json(raw)
    if run_id is not None and critique.run_id != run_id:
        raise ValueError("Critique run_id mismatch")
    if symbol is not None and critique.symbol != symbol:
        raise ValueError("Critique symbol mismatch")
    if research_report_id is not None and critique.research_report_id != research_report_id:
        raise ValueError("Critique research_report_id mismatch")
    if now is not None and abs((critique.as_of_utc - now).total_seconds()) > 900:
        raise ValueError("Critique is stale")
    return critique

class PatrolSignal(Strict):
    symbol: str = Field(min_length=1, max_length=12)
    category: Literal[
        "NEW_LONG", "NEW_SHORT", "BREAKOUT", "BREAKDOWN", "STABILIZATION",
        "REVERSAL", "NEWS_EVENT", "POSITION_RISK", "PLAN_INVALID",
        "BETTER_OPPORTUNITY",
    ]
    direction: Literal["LONG", "SHORT", "NEUTRAL"] = "NEUTRAL"
    severity: Literal["LOW", "MEDIUM", "HIGH"] = "LOW"
    summary: str = Field(min_length=1, max_length=1800)
    evidence_ids: list[str] = Field(default_factory=list, max_length=30)

    @field_validator("evidence_ids", mode="before")
    @classmethod
    def _ids(cls, value):
        if value is None:
            return []
        return [str(item) for item in value if item is not None]


class PatrolReport(Strict):
    schema_version: Literal["1.0"] = "1.0"
    mode: Literal["PATROL"] = "PATROL"
    run_id: str = Field(min_length=1, max_length=120)
    as_of_utc: datetime
    signals: list[PatrolSignal] = Field(default_factory=list, max_length=8)
    notes: str = Field(default="", max_length=1200)

    @field_validator("as_of_utc")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return _utc(value)


def parse_patrol(raw: str, *, run_id: str | None = None, symbols=None,
                 now: datetime | None = None) -> PatrolReport:
    report = PatrolReport.model_validate_json(raw)
    if run_id is not None and report.run_id != run_id:
        raise ValueError("Patrol run_id mismatch")
    if now is not None and abs((report.as_of_utc - now).total_seconds()) > 180:
        raise ValueError("Patrol report is stale")
    if symbols is not None:
        for signal in report.signals:
            if signal.symbol not in symbols:
                raise ValueError("Patrol signal outside fixed watchlist: " + signal.symbol)
    return report
