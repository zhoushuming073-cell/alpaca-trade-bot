import os
from pathlib import Path
from typing import Literal
import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field

ROOT = Path(__file__).resolve().parents[2]
PAPER_URL = "https://paper-api.alpaca.markets"

class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

class Check(Strict):
    indicator: str
    op: Literal["gt", "lt"]
    value: float

class Policy(Strict):
    enabled: bool = False
    version: str
    entry_rules: str = ""
    exit_rules: str = ""
    analysis_rules: str = ""
    allowed_session: Literal["regular"] = "regular"
    history_bars: int = Field(default=40, ge=2, le=500)
    indicators: list[str] = []
    entry_checks: list[Check] = []
    exit_checks: list[Check] = []

    def ready(self):
        return self.enabled and bool(self.entry_rules.strip() and self.exit_rules.strip() and self.analysis_rules.strip())

class Risk(Strict):
    max_symbol_weight: float = Field(gt=0, le=1)
    max_total_weight: float = Field(gt=0, le=1)
    max_daily_loss: float = Field(gt=0, lt=1)
    max_daily_orders: int = Field(gt=0)
    max_bar_age_seconds: int = Field(gt=0, le=900)
    max_quote_age_seconds: int = Field(gt=0, le=60)
    max_spread: float = Field(gt=0, le=.1)
    max_price_deviation: float = Field(gt=0, le=.2)

def config(root=ROOT):
    def read(name):
        return yaml.safe_load((root / "config" / name).read_text(encoding="utf-8"))
    symbols = read("watchlist.yaml")["symbols"]
    import re
    if not isinstance(symbols, list) or len(set(symbols)) != len(symbols) or any(not isinstance(s,str) or not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}",s) for s in symbols):
        raise ValueError("Invalid watchlist")
    return symbols, Policy(**read("strategy.yaml")), Risk(**read("risk.yaml"))

def credentials(root=ROOT):
    load_dotenv(root / ".env", override=False)
    if os.getenv("ALPACA_IS_PAPER", "true").lower() != "true":
        raise ValueError("Paper-only: ALPACA_IS_PAPER must be true")
    return {name: os.getenv(name, "") for name in ("ALPACA_API_KEY", "ALPACA_API_SECRET", "DEEPSEEK_API_KEY")}
