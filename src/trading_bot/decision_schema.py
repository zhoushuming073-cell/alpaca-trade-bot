from typing import Literal
from pydantic import Field
from .settings import Strict

class Decision(Strict):
    symbol: str
    action: Literal["BUY", "SELL", "HOLD"]
    target_weight: float = Field(ge=0, le=1)
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(min_length=1, max_length=4000)
    risk_assessment: str = Field(max_length=4000)

class Response(Strict):
    decisions: list[Decision]

def parse(raw, symbols):
    result = Response.model_validate_json(raw)
    names = [d.symbol for d in result.decisions]
    if len(names) != len(set(names)) or set(names) != set(symbols):
        raise ValueError("Decisions must cover exactly the watchlist")
    return result.decisions
