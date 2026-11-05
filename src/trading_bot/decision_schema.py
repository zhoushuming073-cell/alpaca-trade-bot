"""Versioned, strict AI schemas.

The model is the strategy owner, so every authorization is parsed as untrusted
data.  Unknown fields, ambiguous values, percentages instead of whole shares,
stale plans and missing stop parameters all fail closed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import Field, field_validator, model_validator

from .settings import Strict


class SchemaError(ValueError):
    pass


class SetupType(StrEnum):
    PULLBACK_RECLAIM = "PULLBACK_RECLAIM"
    EVENT_MISPRICING = "EVENT_MISPRICING"


class OpportunityGrade(StrEnum):
    A = "A"
    B = "B"
    C = "C"


class TradeDecision(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    REDUCE = "REDUCE"
    HOLD = "HOLD"
    WAIT = "WAIT"
    NO_TRADE = "NO_TRADE"


class StopPolicy(StrEnum):
    NONE = "NONE"
    BROKER_STOP = "BROKER_STOP"


class TriggerPurpose(StrEnum):
    EXECUTE_ORDER = "EXECUTE_ORDER"
    REQUEST_AI_RECHECK = "REQUEST_AI_RECHECK"
    INVALIDATE_PLAN = "INVALIDATE_PLAN"
    MONITOR_ONLY = "MONITOR_ONLY"


class ConditionOp(StrEnum):
    LE = "<="
    LT = "<"
    GE = ">="
    GT = ">"
    EQ = "=="


class ConditionField(StrEnum):
    LAST = "last"
    ASK = "ask"
    BID = "bid"
    SPREAD = "spread"
    SPREAD_BPS = "spread_bps"

    @classmethod
    def _missing_(cls, value):
        aliases = {
            "last_trade_price": cls.LAST,
            "last_price": cls.LAST,
            "ask_price": cls.ASK,
            "bid_price": cls.BID,
        }
        if isinstance(value, str) and value.lower() in aliases:
            return aliases[value.lower()]
        return None


class MarketDecision(StrEnum):
    WATCH = "WATCH"
    NO_TRADE = "NO_TRADE"
    TRADE = "TRADE"


class RecheckDecision(StrEnum):
    CONTINUE = "CONTINUE"
    REPLACE = "REPLACE"
    INVALIDATE = "INVALIDATE"
    NO_TRADE = "NO_TRADE"


class PositionAction(StrEnum):
    HOLD = "HOLD"
    REDUCE = "REDUCE"
    SELL = "SELL"
    ADJUST_STOP = "ADJUST_STOP"
    REMOVE_STOP = "REMOVE_STOP"
    NO_STOP = "NO_STOP"


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("UTC timestamp required")
    return value.astimezone(timezone.utc)


class Condition(Strict):
    field: ConditionField
    op: ConditionOp
    value: float


class Trigger(Strict):
    trigger_id: str = Field(min_length=1, max_length=80)
    purpose: TriggerPurpose
    all_of: list[Condition] = Field(min_length=1, max_length=8)


class StopLoss(Strict):
    stop_price: Decimal = Field(gt=0)


class OrderIntent(Strict):
    action: Literal["BUY", "SELL"]
    symbol: str = Field(min_length=1, max_length=12)
    qty: int = Field(gt=0, strict=True)
    order_type: Literal["limit"] = "limit"
    limit_price: Decimal = Field(gt=0)
    time_in_force: Literal["day"] = "day"
    extended_hours: Literal[False] = False
    stop_policy: StopPolicy = StopPolicy.NONE
    stop_loss: StopLoss | None = None

    @field_validator("limit_price")
    @classmethod
    def _price_precision(cls, value: Decimal) -> Decimal:
        if value.as_tuple().exponent < -2:
            raise ValueError("limit_price supports at most two decimals")
        return value

    @model_validator(mode="after")
    def _stop_consistency(self):
        if self.stop_policy == StopPolicy.BROKER_STOP:
            if self.action != "BUY":
                raise ValueError("BROKER_STOP is only supported for BUY intents")
            if self.stop_loss is None:
                raise ValueError("BROKER_STOP requires stop_loss")
            if self.stop_loss.stop_price >= self.limit_price:
                raise ValueError("BUY stop must be below the limit price")
        elif self.stop_loss is not None:
            raise ValueError("NONE stop policy must not carry stop_loss")
        return self


class Candidate(Strict):
    symbol: str = Field(min_length=1, max_length=12)
    setup_type: SetupType
    opportunity_grade: OpportunityGrade
    trade_decision: TradeDecision
    analysis: str = Field(min_length=1, max_length=6000)
    evidence_ids: list[str] = Field(default_factory=list, max_length=50)
    risk_flags: list[str] = Field(default_factory=list, max_length=30)
    veto: bool = False
    plan_id: str = Field(min_length=1, max_length=100)
    plan_valid_until_utc: datetime
    triggers: list[Trigger] = Field(default_factory=list, max_length=12)
    order_intent: OrderIntent | None = None
    invalidation: list[Condition] = Field(default_factory=list, max_length=8)

    @field_validator("evidence_ids", mode="before")
    @classmethod
    def _evidence_ids_are_strings(cls, value):
        if value is None:
            return []
        return [str(item) for item in value if item is not None]

    @field_validator("plan_valid_until_utc")
    @classmethod
    def _utc_valid(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def _decision_consistency(self):
        if self.veto:
            return self
        if self.trade_decision in {TradeDecision.BUY, TradeDecision.SELL, TradeDecision.REDUCE}:
            if self.order_intent is None:
                raise ValueError("trading candidates require order_intent")
            expected = "BUY" if self.trade_decision is TradeDecision.BUY else "SELL"
            if self.order_intent.action != expected:
                raise ValueError("order_intent.action does not match trade_decision")
            if self.order_intent.symbol != self.symbol:
                raise ValueError("order_intent.symbol does not match candidate")
        else:
            if self.order_intent is not None:
                raise ValueError("non-trading candidates must not carry order_intent")
            if any(t.purpose is TriggerPurpose.EXECUTE_ORDER for t in self.triggers):
                raise ValueError("EXECUTE_ORDER requires a trading candidate with order_intent")
        trigger_ids = [trigger.trigger_id for trigger in self.triggers]
        if len(trigger_ids) != len(set(trigger_ids)):
            raise ValueError("duplicate trigger_id")
        return self


class PremarketPlan(Strict):
    schema_version: Literal["2.0"] = "2.0"
    mode: Literal["PREMARKET_PLAN"] = "PREMARKET_PLAN"
    as_of_utc: datetime
    model_id: str = Field(min_length=1, max_length=80)
    market_decision: MarketDecision
    account_snapshot_id: str = Field(min_length=1, max_length=120)
    plan_id: str | None = Field(default=None, max_length=100)
    candidates: list[Candidate] = Field(default_factory=list, max_length=24)

    @field_validator("as_of_utc")
    @classmethod
    def _utc_valid(cls, value: datetime) -> datetime:
        return _utc(value)


class ScanCandidate(Strict):
    symbol: str = Field(min_length=1, max_length=12)
    setup_type: SetupType
    opportunity_grade: OpportunityGrade
    trade_decision: TradeDecision
    thesis: str = Field(min_length=1, max_length=2000)
    evidence_ids: list[str] = Field(default_factory=list, max_length=30)
    priority: int = Field(ge=1, le=12)

    @field_validator("evidence_ids", mode="before")
    @classmethod
    def _evidence_ids_are_strings(cls, value):
        if value is None:
            return []
        return [str(item) for item in value if item is not None]


class UniverseScan(Strict):
    schema_version: Literal["2.0"] = "2.0"
    mode: Literal["UNIVERSE_SCAN"] = "UNIVERSE_SCAN"
    as_of_utc: datetime
    model_id: str
    account_snapshot_id: str
    candidates: list[ScanCandidate] = Field(default_factory=list, max_length=12)
    skipped_symbols: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("as_of_utc")
    @classmethod
    def _utc_valid(cls, value: datetime) -> datetime:
        return _utc(value)


class RecheckResponse(Strict):
    schema_version: Literal["2.0"] = "2.0"
    mode: Literal["INTRADAY_RECHECK"] = "INTRADAY_RECHECK"
    as_of_utc: datetime
    plan_id: str
    decision: RecheckDecision
    reason: str = Field(min_length=1, max_length=4000)
    order_intent: OrderIntent | None = None
    plan_valid_until_utc: datetime | None = None
    invalidation: list[Condition] = Field(default_factory=list, max_length=8)

    @field_validator("as_of_utc", "plan_valid_until_utc")
    @classmethod
    def _utc_valid(cls, value: datetime | None) -> datetime | None:
        return _utc(value) if value is not None else None

    @model_validator(mode="after")
    def _decision_consistency(self):
        if self.decision in {RecheckDecision.CONTINUE, RecheckDecision.INVALIDATE, RecheckDecision.NO_TRADE}:
            if self.order_intent is not None:
                raise ValueError("order_intent only allowed with REPLACE")
        if self.decision is RecheckDecision.REPLACE and self.order_intent is None:
            raise ValueError("REPLACE requires order_intent")
        return self


class PositionInstruction(Strict):
    symbol: str
    action: PositionAction
    qty: int | None = Field(default=None, gt=0, strict=True)
    limit_price: Decimal | None = Field(default=None, gt=0)
    stop_policy: StopPolicy = StopPolicy.NONE
    stop_loss: StopLoss | None = None
    plan_id: str | None = None
    reason: str = Field(min_length=1, max_length=3000)

    @model_validator(mode="after")
    def _shape(self):
        if self.action in {PositionAction.SELL, PositionAction.REDUCE}:
            if self.qty is None or self.limit_price is None:
                raise ValueError("sell/reduce requires qty and limit_price")
            if self.stop_policy is StopPolicy.BROKER_STOP:
                raise ValueError("BROKER_STOP is not supported for exits in this build")
        if self.action in {PositionAction.ADJUST_STOP, PositionAction.REMOVE_STOP}:
            if not self.plan_id:
                raise ValueError("stop changes require the existing plan/order reference")
        return self


class PositionReview(Strict):
    schema_version: Literal["2.0"] = "2.0"
    mode: Literal["POSITION_REVIEW"] = "POSITION_REVIEW"
    as_of_utc: datetime
    instructions: list[PositionInstruction] = Field(default_factory=list, max_length=100)

    @field_validator("as_of_utc")
    @classmethod
    def _utc_valid(cls, value: datetime) -> datetime:
        return _utc(value)


class Evidence(Strict):
    evidence_id: str | None = None
    kind: Literal["filing", "news", "ir", "macro", "market", "other"] = "other"
    source_url: str | None = None
    title: str | None = None
    published_at_utc: datetime | None = None
    retrieved_at_utc: datetime | None = None
    available_at_utc: datetime | None = None
    verified: bool = False
    summary: str = Field(default="", max_length=2000)

    @field_validator("published_at_utc", "retrieved_at_utc", "available_at_utc")
    @classmethod
    def _utc_valid(cls, value: datetime | None) -> datetime | None:
        return _utc(value) if value is not None else None


class ResearchRefresh(Strict):
    schema_version: Literal["2.0"] = "2.0"
    mode: Literal["RESEARCH_REFRESH"] = "RESEARCH_REFRESH"
    symbol: str = Field(min_length=1, max_length=12)
    as_of_utc: datetime
    status: Literal["OK", "STALE", "UNKNOWN", "SOURCE_UNAVAILABLE"]
    facts: list[str] = Field(default_factory=list, max_length=50)
    interpretations: list[str] = Field(default_factory=list, max_length=50)
    assumptions: list[str] = Field(default_factory=list, max_length=50)
    unknowns: list[str] = Field(default_factory=list, max_length=50)
    key_levels: dict[str, float] = Field(default_factory=dict)
    invalidation: list[str] = Field(default_factory=list, max_length=20)
    evidence: list[Evidence] = Field(default_factory=list, max_length=50)
    expires_at_utc: datetime | None = None
    profile_version: int = Field(default=1, ge=1)

    @field_validator("as_of_utc", "expires_at_utc")
    @classmethod
    def _utc_valid(cls, value: datetime | None) -> datetime | None:
        return _utc(value) if value is not None else None


def parse_plan(raw: str, *, symbols=None, now: datetime | None = None) -> PremarketPlan:
    try:
        plan = PremarketPlan.model_validate_json(raw)
    except Exception as exc:
        raise SchemaError(f"Invalid premarket plan: {exc}") from exc
    validate_plan(plan, symbols=symbols, now=now)
    return plan


def validate_plan(plan: PremarketPlan, *, symbols=None, now: datetime | None = None) -> None:
    now = _utc(now or datetime.now(timezone.utc))
    seen_symbols = set()
    seen_plan_ids = set()
    for candidate in plan.candidates:
        if symbols is not None and candidate.symbol not in symbols:
            raise SchemaError(f"Candidate outside fixed watchlist: {candidate.symbol}")
        if candidate.symbol in seen_symbols:
            raise SchemaError(f"Duplicate candidate symbol: {candidate.symbol}")
        seen_symbols.add(candidate.symbol)
        if candidate.plan_id in seen_plan_ids:
            raise SchemaError(f"Duplicate plan_id: {candidate.plan_id}")
        seen_plan_ids.add(candidate.plan_id)
        if candidate.plan_valid_until_utc <= now:
            raise SchemaError(f"Plan already expired: {candidate.plan_id}")
        for trigger in candidate.triggers:
            values = [condition.value for condition in trigger.all_of]
            if not all(value == value and abs(value) != float("inf") for value in values):
                raise SchemaError("Non-finite trigger value")
    if plan.market_decision is MarketDecision.NO_TRADE and any(
        candidate.order_intent for candidate in plan.candidates
    ):
        raise SchemaError("NO_TRADE plan must not contain order intents")


def parse_scan(raw: str, *, symbols=None, now: datetime | None = None) -> UniverseScan:
    try:
        scan = UniverseScan.model_validate_json(raw)
    except Exception as exc:
        raise SchemaError(f"Invalid universe scan: {exc}") from exc
    if symbols is not None:
        for candidate in scan.candidates:
            if candidate.symbol not in symbols:
                raise SchemaError(f"Scan candidate outside fixed watchlist: {candidate.symbol}")
    if len({c.symbol for c in scan.candidates}) != len(scan.candidates):
        raise SchemaError("Duplicate scan candidate")
    return scan


def parse_recheck(raw: str, *, plan_id: str | None = None, now: datetime | None = None) -> RecheckResponse:
    try:
        result = RecheckResponse.model_validate_json(raw)
    except Exception as exc:
        raise SchemaError(f"Invalid recheck: {exc}") from exc
    if plan_id is not None and result.plan_id != plan_id:
        raise SchemaError("Recheck plan_id mismatch")
    return result


def parse_position_review(raw: str, *, symbols=None) -> PositionReview:
    try:
        review = PositionReview.model_validate_json(raw)
    except Exception as exc:
        raise SchemaError(f"Invalid position review: {exc}") from exc
    if symbols is not None:
        for instruction in review.instructions:
            if instruction.symbol not in symbols:
                raise SchemaError("Position review outside fixed watchlist")
    return review


def parse_research(raw: str, *, symbol: str | None = None) -> ResearchRefresh:
    try:
        profile = ResearchRefresh.model_validate_json(raw)
    except Exception as exc:
        raise SchemaError(f"Invalid research refresh: {exc}") from exc
    if symbol is not None and profile.symbol != symbol:
        raise SchemaError("Research symbol mismatch")
    return profile


def as_intent(candidate: Candidate) -> dict | None:
    if candidate.order_intent is None:
        return None
    intent = candidate.order_intent.model_dump(mode="json")
    intent["side"] = intent.pop("action").lower()
    intent["stop_policy"] = str(intent.get("stop_policy") or "NONE")
    return intent
