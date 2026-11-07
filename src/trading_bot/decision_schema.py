"""Strict schemas used by the production OneShotEngine and research service."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import Field, field_validator, model_validator

from .settings import Strict


class SchemaError(ValueError):
    pass



class OrderAction(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    REDUCE = "REDUCE"
    OPEN_LONG = "OPEN_LONG"
    CLOSE_LONG = "CLOSE_LONG"
    OPEN_SHORT = "OPEN_SHORT"
    COVER_SHORT = "COVER_SHORT"

    @property
    def side(self) -> str:
        return "buy" if self in {
            OrderAction.BUY, OrderAction.OPEN_LONG, OrderAction.COVER_SHORT,
        } else "sell"

    @property
    def position_intent(self) -> str:
        if self in {OrderAction.BUY, OrderAction.OPEN_LONG}:
            return "buy_to_open"
        if self in {OrderAction.SELL, OrderAction.REDUCE, OrderAction.CLOSE_LONG}:
            return "sell_to_close"
        if self is OrderAction.OPEN_SHORT:
            return "sell_to_open"
        return "buy_to_close"



class StopPolicy(StrEnum):
    NONE = "NONE"
    BROKER_STOP = "BROKER_STOP"



def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("UTC timestamp required")
    return value.astimezone(timezone.utc)



class StopLoss(Strict):
    stop_price: Decimal = Field(gt=0)



class OrderIntent(Strict):
    action: OrderAction
    symbol: str = Field(min_length=1, max_length=12)
    qty: int = Field(gt=0, strict=True)
    order_type: Literal["limit"] = "limit"
    limit_price: Decimal = Field(gt=0)
    time_in_force: Literal["day"] = "day"
    extended_hours: Literal[False] = False
    stop_policy: StopPolicy = StopPolicy.NONE
    stop_loss: StopLoss | None = None

    @property
    def side(self) -> str:
        return self.action.side

    @property
    def position_intent(self) -> str:
        return self.action.position_intent

    @field_validator("limit_price")
    @classmethod
    def _price_precision(cls, value: Decimal) -> Decimal:
        if value.as_tuple().exponent < -2:
            raise ValueError("limit_price supports at most two decimals")
        return value

    @model_validator(mode="after")
    def _stop_consistency(self):
        if self.stop_policy == StopPolicy.BROKER_STOP:
            if self.action not in {
                OrderAction.BUY, OrderAction.OPEN_LONG, OrderAction.OPEN_SHORT,
            }:
                raise ValueError("BROKER_STOP is only supported for opening intents")
            if self.stop_loss is None:
                raise ValueError("BROKER_STOP requires stop_loss")
            if self.side == "buy" and self.stop_loss.stop_price >= self.limit_price:
                raise ValueError("BUY stop must be below the limit price")
            if self.side == "sell" and self.stop_loss.stop_price <= self.limit_price:
                raise ValueError("SELL short stop must be above the limit price")
        elif self.stop_loss is not None:
            raise ValueError("NONE stop policy must not carry stop_loss")
        return self



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



def parse_research(raw: str, *, symbol: str | None = None) -> ResearchRefresh:
    try:
        profile = ResearchRefresh.model_validate_json(raw)
    except Exception as exc:
        raise SchemaError(f"Invalid research refresh: {exc}") from exc
    if symbol is not None and profile.symbol != symbol:
        raise SchemaError("Research symbol mismatch")
    return profile



def as_order_intent(order_intent: OrderIntent) -> dict:
    intent = order_intent.model_dump(mode="json")
    intent["action"] = order_intent.action.value
    intent["side"] = order_intent.side
    intent["position_intent"] = order_intent.position_intent
    intent["stop_policy"] = str(intent.get("stop_policy") or "NONE")
    return intent
