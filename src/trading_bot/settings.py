"""Configuration models, hashing and credential loading.

Two views of configuration are deliberately distinct:

* **disk configuration** — what the files currently contain;
* **active configuration** — the immutable snapshot a running worker wrote to
  SQLite at startup.

A changed disk hash never silently changes a running process.  The worker
records ``CONFIG_PENDING_RESTART`` and freezes new risk until a safe restart.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, field_validator

ROOT = Path(__file__).resolve().parents[2]
PAPER_URL = "https://paper-api.alpaca.markets"


def paper_db_path():
    """Keep live trading data outside the Git checkout, including on Windows."""
    path = ROOT.parent / (ROOT.name + "-runtime") / "paper.sqlite"
    if not path.exists() and (ROOT / "data/paper.sqlite").exists():
        raise RuntimeError("External runtime database missing: migrate the existing database before starting; refusing an empty replacement")
    return path


def backup_directory():
    return ROOT.parent / (ROOT.name + "-backups")


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
        return self.enabled and bool(
            self.entry_rules.strip() and self.exit_rules.strip() and self.analysis_rules.strip()
        )


class Risk(Strict):
    """Non-positional protections that remain enabled.

    The previous 10% single-symbol and 50% total-portfolio caps are deliberately
    removed by the latest user specification.  Cash, no-margin, no-short and
    duplicate-order checks are enforced in ``risk_manager`` instead.
    """

    max_daily_loss: float = Field(default=0.02, gt=0, lt=1)
    max_daily_orders: int = Field(default=10, gt=0)
    max_bar_age_seconds: int = Field(default=120, gt=0, le=900)
    max_quote_age_seconds: int = Field(default=30, gt=0, le=120)
    max_spread: float = Field(default=0.02, gt=0, le=0.2)
    max_price_deviation: float = Field(default=0.05, gt=0, le=0.25)


def _validate_hhmm(value: str) -> str:
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d)?", value):
        raise ValueError("time must be HH:MM or HH:MM:SS")
    return value


class Execution(Strict):
    """Execution contract and session windows (America/New_York)."""

    mode: Literal["paper"] = "paper"
    timezone: Literal["America/New_York"] = "America/New_York"
    premarket_start: str = "09:00"
    active_start: str = "09:30"
    active_end: str = "10:00"
    submit_cutoff: str = "09:59:30"
    order_type: Literal["limit"] = "limit"
    time_in_force: Literal["day"] = "day"
    extended_hours: Literal[False] = False
    # Broker-side stops are only enabled after the exact account/path has been
    # verified against Alpaca Paper.  Until then a requested BROKER_STOP is
    # rejected; it is never downgraded to NONE.
    allow_broker_stop: bool = False
    broker_stop_verified: bool = False
    trigger_poll_seconds: int = Field(default=10, ge=2, le=60)
    submission_grace_seconds: int = Field(default=30, ge=5, le=300)
    recheck_cooldown_seconds: int = Field(default=120, ge=30, le=1800)
    max_rechecks_per_day: int = Field(default=20, ge=1, le=200)
    watchdog_interval_seconds: int = Field(default=10, ge=2, le=120)
    watchdog_stale_seconds: int = Field(default=45, ge=10, le=600)
    data_workers: int = Field(default=8, ge=1, le=16)
    premarket_retry_seconds: int = Field(default=300, ge=30, le=1800)
    ai_max_tokens: int = Field(default=24000, ge=1000, le=200000)
    ai_temperature: float = Field(default=0.2, ge=0, le=1)
    ai_repair_attempts: int = Field(default=1, ge=0, le=1)
    research_search_enabled: bool = False
    research_fetch_enabled: bool = False

    @field_validator("premarket_start", "active_start", "active_end", "submit_cutoff")
    @classmethod
    def _time(cls, value: str) -> str:
        return _validate_hhmm(value)


@dataclass(frozen=True)
class ConfigBundle:
    symbols: tuple[str, ...]
    policy: Policy
    risk: Risk
    execution: Execution
    hashes: dict[str, str]
    raw: dict[str, dict]

    @property
    def hash(self) -> str:
        return self.hashes["combined"]

    def __iter__(self):
        # Old callers unpack ``symbols, policy, risk = config()``.
        yield self.symbols
        yield self.policy
        yield self.risk

    def __getitem__(self, index):
        return (self.symbols, self.policy, self.risk, self.execution)[index]


def _read(root: Path, name: str) -> dict:
    raw = (root / "config" / name).read_text(encoding="utf-8")
    data = yaml.safe_load(raw)
    if not isinstance(data, dict):
        raise ValueError(f"config/{name} must be a mapping")
    return data


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _sha(value) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()[:16]


def _validate_symbols(symbols) -> tuple[str, ...]:
    if not isinstance(symbols, list):
        raise ValueError("Invalid watchlist")
    cleaned = []
    for symbol in symbols:
        if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", symbol):
            raise ValueError("Invalid watchlist symbol")
        cleaned.append(symbol)
    if len(set(cleaned)) != len(cleaned):
        raise ValueError("Invalid watchlist: duplicate symbols")
    return tuple(cleaned)


def load_config(root: Path | str = ROOT) -> ConfigBundle:
    root = Path(root)
    symbols = _validate_symbols(_read(root, "watchlist.yaml").get("symbols"))
    policy = Policy(**_read(root, "strategy.yaml"))
    risk = Risk(**_read(root, "risk.yaml"))
    execution = Execution(**_read(root, "execution.yaml"))
    raw = {
        "watchlist": _read(root, "watchlist.yaml"),
        "strategy": _read(root, "strategy.yaml"),
        "risk": _read(root, "risk.yaml"),
        "execution": _read(root, "execution.yaml"),
    }
    hashes = {
        "watchlist": _sha(list(symbols)),
        "strategy": _sha(policy.model_dump()),
        "risk": _sha(risk.model_dump()),
        "execution": _sha(execution.model_dump()),
    }
    hashes["combined"] = _sha(hashes)
    return ConfigBundle(symbols, policy, risk, execution, hashes, raw)


def config(root: Path | str = ROOT):
    """Backward-compatible tuple view for older call sites/tests."""
    bundle = load_config(root)
    return bundle.symbols, bundle.policy, bundle.risk


def config_snapshot(bundle: ConfigBundle) -> dict:
    return {
        "symbols": list(bundle.symbols),
        "hashes": dict(bundle.hashes),
        "version": bundle.policy.version,
        "execution": bundle.execution.model_dump(),
        "policy": {
            "version": bundle.policy.version,
            "enabled": bundle.policy.enabled,
        },
    }


def credentials(root: Path | str = ROOT):
    root = Path(root)
    load_dotenv(root / ".env", override=False)
    if os.getenv("ALPACA_IS_PAPER", "true").lower() != "true":
        raise ValueError("Paper-only: ALPACA_IS_PAPER must be true")
    return {
        name: os.getenv(name, "")
        for name in ("ALPACA_API_KEY", "ALPACA_API_SECRET", "DEEPSEEK_API_KEY")
    }


def paper_endpoint() -> str:
    return PAPER_URL
