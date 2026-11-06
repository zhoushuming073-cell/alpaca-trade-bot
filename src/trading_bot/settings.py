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
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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
    removed by the latest user specification.  Long cash, short buying-power,
    duplicate-order and no-cross-zero checks are enforced in ``risk_manager``.
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
    active_end: str = "12:00"
    submit_cutoff: str = "11:59:30"
    order_type: Literal["limit"] = "limit"
    time_in_force: Literal["day"] = "day"
    extended_hours: Literal[False] = False
    # Broker-side stops are only enabled after the exact account/path has been
    # verified against Alpaca Paper.  Until then a requested BROKER_STOP is
    # rejected; it is never downgraded to NONE.
    allow_broker_stop: bool = False
    broker_stop_verified: bool = False
    # Shorting is an explicit, default-off capability.  Even when true, every
    # OPEN_SHORT still requires account shorting_enabled plus live asset
    # shortable/easy_to_borrow checks from Alpaca.
    allow_shorting: bool = False
    trigger_poll_seconds: int = Field(default=10, ge=2, le=60)
    active_research_interval_seconds: int = Field(default=900, ge=60, le=3600)
    active_research_min_cooldown_seconds: int = Field(default=120, ge=30, le=1800)
    active_research_max_tokens: int | None = Field(default=None, ge=1000, le=5_000_000)
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


class AgentRoleConfig(Strict):
    enabled: bool = True
    model: str = Field(default="deepseek-flash", min_length=1, max_length=80)
    max_tokens: int = Field(default=6000, ge=256, le=64000)
    timeout_seconds: int = Field(default=45, ge=5, le=300)
    max_retries: int = Field(default=1, ge=0, le=3)
    temperature: float = Field(default=0.2, ge=0, le=1)


class AgentsConfig(Strict):
    researcher: AgentRoleConfig = Field(default_factory=AgentRoleConfig)
    skeptic: AgentRoleConfig = Field(default_factory=AgentRoleConfig)
    portfolio_manager: AgentRoleConfig = Field(default_factory=AgentRoleConfig)
    baseline: AgentRoleConfig = Field(
        default_factory=lambda: AgentRoleConfig(max_tokens=8000, timeout_seconds=60)
    )


class MultiAgentExecution(Strict):
    paper_only: Literal[True] = True
    require_complete_decision: bool = True
    max_deep_candidates: int = Field(default=6, ge=0, le=16)
    max_parallel_requests: int = Field(default=2, ge=1, le=4)
    deadline_buffer_seconds: int = Field(default=45, ge=0, le=600)


class AgentBudget(Strict):
    # null is not unlimited: effective_* falls back to explicit finite defaults.
    max_session_tokens: int | None = Field(default=None, ge=1000)
    max_session_cost_usd: float | None = Field(default=None, ge=0)
    default_session_tokens: int = Field(default=120000, ge=1000, le=2_000_000)
    default_session_cost_usd: float = Field(default=1.0, ge=0, le=100)
    cost_per_1m_input_usd: float = Field(default=0.0, ge=0, le=1000)
    cost_per_1m_output_usd: float = Field(default=0.0, ge=0, le=1000)

    @property
    def effective_max_tokens(self) -> int | None:
        # Explicit null means unlimited during the current test phase.
        return None if self.max_session_tokens is None else int(self.max_session_tokens)

    @property
    def effective_max_cost(self) -> float:
        if self.max_session_cost_usd is None:
            return float(self.default_session_cost_usd)
        return float(self.max_session_cost_usd)


class AgentComparison(Strict):
    enable_single_agent_baseline: bool = True
    # The baseline is shadow-only in this build. A true value is rejected at
    # config load rather than silently creating a second trading path.
    baseline_can_trade: Literal[False] = False


class MultiAgent(Strict):
    enabled: bool = False
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    execution: MultiAgentExecution = Field(default_factory=MultiAgentExecution)
    budget: AgentBudget = Field(default_factory=AgentBudget)
    comparison: AgentComparison = Field(default_factory=AgentComparison)

    @model_validator(mode="after")
    def _baseline_is_shadow_only(self):
        if self.comparison.baseline_can_trade:
            raise ValueError("The single-agent baseline cannot trade")
        return self


@dataclass(frozen=True)
class ConfigBundle:
    symbols: tuple[str, ...]
    policy: Policy
    risk: Risk
    execution: Execution
    multi_agent: MultiAgent
    hashes: dict[str, str]
    raw: dict[str, dict]

    @property
    def hash(self) -> str:
        return self.hashes["combined"]

    @property
    def groups(self) -> tuple[dict, ...]:
        watchlist = self.raw.get("watchlist") or {}
        normalized = _validate_groups(watchlist.get("groups"), self.symbols)
        return tuple(
            {"id": group["id"], "name": group["name"], "symbols": tuple(group["symbols"])}
            for group in normalized
        )

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


def _validate_groups(groups, symbols) -> tuple[dict, ...]:
    """Validate that the research groups partition the fixed watchlist exactly."""
    if not isinstance(groups, list) or not groups:
        raise ValueError("Invalid watchlist: groups required")
    seen_ids = set()
    seen_names = set()
    members = []
    cleaned = []
    for group in groups:
        if not isinstance(group, dict):
            raise ValueError("Invalid watchlist group")
        identifier = group.get("id")
        name = group.get("name")
        members_list = group.get("symbols")
        if isinstance(identifier, bool) or not isinstance(identifier, int) or identifier <= 0:
            raise ValueError("Invalid group id")
        if identifier in seen_ids:
            raise ValueError("Duplicate group id")
        if not isinstance(name, str) or not name.strip() or name.strip() in seen_names:
            raise ValueError("Invalid or duplicate group name")
        if not isinstance(members_list, list) or not members_list:
            raise ValueError("Invalid group symbols")
        for symbol in members_list:
            if symbol not in symbols:
                raise ValueError("Group symbol outside watchlist: " + str(symbol))
        if len(set(members_list)) != len(members_list):
            raise ValueError("Duplicate symbol inside group")
        seen_ids.add(identifier)
        seen_names.add(name.strip())
        members.extend(members_list)
        cleaned.append({
            "id": identifier,
            "name": name.strip(),
            "symbols": tuple(members_list),
        })
    if len(members) != len(set(members)):
        raise ValueError("A symbol appears in more than one group")
    if set(members) != set(symbols) or len(members) != len(symbols):
        missing = sorted(set(symbols) - set(members))
        extra = sorted(set(members) - set(symbols))
        raise ValueError(f"Groups do not partition watchlist: missing={missing} extra={extra}")
    return tuple(cleaned)


def load_config(root: Path | str = ROOT) -> ConfigBundle:
    root = Path(root)
    watchlist = _read(root, "watchlist.yaml")
    symbols = _validate_symbols(watchlist.get("symbols"))
    groups = _validate_groups(watchlist.get("groups"), symbols)
    policy = Policy(**_read(root, "strategy.yaml"))
    risk = Risk(**_read(root, "risk.yaml"))
    execution = Execution(**_read(root, "execution.yaml"))
    multi_agent = MultiAgent(**_read(root, "multi_agent.yaml"))
    raw = {
        "watchlist": watchlist,
        "strategy": _read(root, "strategy.yaml"),
        "risk": _read(root, "risk.yaml"),
        "execution": _read(root, "execution.yaml"),
        "multi_agent": _read(root, "multi_agent.yaml"),
    }
    hashes = {
        "watchlist": _sha({"symbols": list(symbols), "groups": list(groups)}),
        "strategy": _sha(policy.model_dump()),
        "risk": _sha(risk.model_dump()),
        "execution": _sha(execution.model_dump()),
        "multi_agent": _sha(multi_agent.model_dump()),
    }
    hashes["combined"] = _sha(hashes)
    return ConfigBundle(symbols, policy, risk, execution, multi_agent, hashes, raw)


def config(root: Path | str = ROOT):
    """Backward-compatible tuple view for older call sites/tests."""
    bundle = load_config(root)
    return bundle.symbols, bundle.policy, bundle.risk


def config_snapshot(bundle: ConfigBundle) -> dict:
    return {
        "symbols": list(bundle.symbols),
        "groups": [
            {"id": group["id"], "name": group["name"], "symbols": list(group["symbols"])}
            for group in bundle.groups
        ],
        "hashes": dict(bundle.hashes),
        "version": bundle.policy.version,
        "execution": bundle.execution.model_dump(),
        "multi_agent": bundle.multi_agent.model_dump(),
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
