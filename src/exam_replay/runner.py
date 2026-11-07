"""Point-in-time exam replay for the existing AI decision contract.

This module is intentionally outside the production broker/research path.  It
reads only the frozen DuckDB snapshot, constructs a compact 09:00 ET packet,
calls one tool-free DeepSeek transport per stage, and sends approved decisions
to the isolated HistoricalSimBroker.  It never imports Alpaca, the live Store,
web search or production research memory.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from exam_data import ExamStore
from exam_data.store import ET
from exam_v2.evaluation import evaluate
from exam_v2.simulation import HistoricalSimBroker
from exam_v2.snapshot import ExamMode, get_v2_snapshot

from exam_replay.legacy_decision_schema import (
    MarketDecision, OrderIntent, PremarketPlan, TradeDecision, parse_plan, parse_scan,
)

ROOT = Path(__file__).resolve().parents[2]
EXAM_DB = ROOT / "data" / "exam_v2.duckdb"
DEFAULT_MODEL = "deepseek-flash"
EXAM_GUARD = (
    "HISTORICAL EXAM ONLY. Use only the dated JSON input below. No web search, "
    "fetch tools, live account, production research memory, current news, or "
    "later price knowledge. Treat UNKNOWN material as uncertain. The broker is an "
    "isolated simulator; no real orders. The current fixed watchlist and strategy "
    "were selected after these dates and may cause selection bias. Do not infer "
    "a short merely from a falling price.\n\n"
)


def _canon(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _sha(value):
    return hashlib.sha256(_canon(value).encode("utf-8")).hexdigest()


def _parse_time(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)


def _bar(bar):
    return {
        "ts": bar["ts"].astimezone(timezone.utc).isoformat(),
        "end_at": bar["end_at"].astimezone(timezone.utc).isoformat(),
        "o": float(bar["open"]), "h": float(bar["high"]),
        "l": float(bar["low"]), "c": float(bar["close"]),
        "v": int(bar["volume"]),
    }


def _returns(daily):
    closes = [float(bar["close"]) for bar in daily]
    def ret(period):
        return round((closes[-1] / closes[-1 - period] - 1) * 100, 3) if len(closes) > period else None
    return {
        "last": closes[-1] if closes else None,
        "change_1d_pct": ret(1), "change_5d_pct": ret(5), "change_20d_pct": ret(20),
        "high_20": max((float(bar["high"]) for bar in daily[-20:]), default=None),
        "low_20": min((float(bar["low"]) for bar in daily[-20:]), default=None),
    }


@dataclass
class ExamCall:
    stage: str
    day: str
    system_sha256: str
    user_sha256: str
    raw: str
    parsed: dict | None
    prompt_tokens: int | None
    completion_tokens: int | None
    latency_ms: int
    status: str
    error: str = ""


@dataclass
class ReplayResult:
    days: list[dict] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)
    trades: list[dict] = field(default_factory=list)
    fills: list[dict] = field(default_factory=list)
    open_positions: list[dict] = field(default_factory=list)
    decisions: list[dict] = field(default_factory=list)
    calls: list[dict] = field(default_factory=list)
    qqq_prices: list[float] = field(default_factory=list)
    simulator: HistoricalSimBroker | None = None
    start_cash: float = 100000.0
    short_locate_assumption: str = "ALL_SYMBOLS_ASSUMED_AVAILABLE_FOR_EXAM"

    def metrics(self):
        if len(self.equity_curve) < 2:
            return {}
        gross = [day.get("gross_exposure", 0.0) for day in self.days]
        return evaluate(self.equity_curve, self.trades, gross,
                        qqq_prices=self.qqq_prices or None)


class ExamClient:
    """Minimal tool-free JSON transport; no production client/memory import."""

    def __init__(self, *, model=DEFAULT_MODEL, max_tokens=7000, api_key=None, client=None):
        self.model = model
        self.max_tokens = max_tokens
        self._client = client
        self._api_key = api_key

    def _openai(self):
        if self._client is None:
            if not self._api_key:
                raise RuntimeError("DeepSeek API key unavailable")
            from openai import OpenAI
            self._client = OpenAI(
                api_key=self._api_key, base_url="https://api.deepseek.com",
                timeout=120, max_retries=0,
            )
        return self._client

    def __call__(self, system, user):
        started = time.monotonic()
        response = self._openai().chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            response_format={"type": "json_object"},
            max_tokens=self.max_tokens, temperature=0.2,
            extra_body={"thinking": {"type": "disabled"}},
        )
        choice = response.choices[0]
        if choice.finish_reason != "stop":
            raise ValueError("Model response incomplete: " + str(choice.finish_reason))
        usage = response.usage
        return {
            "raw": choice.message.content or "",
            "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0) if usage else None,
            "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0) if usage else None,
            "latency_ms": int((time.monotonic() - started) * 1000),
        }


class ExamReplay:
    def __init__(
        self, *, symbols, db_path=EXAM_DB, mode=ExamMode.EXPLORATORY,
        initial_cash=100000, model=DEFAULT_MODEL, transport=None,
        short_locates=True, max_focus=5, max_tokens=7000, ledger_path=None,
    ):
        self.symbols = list(symbols)
        self.db_path = Path(db_path)
        self.mode = ExamMode(mode)
        self.initial_cash = Decimal(str(initial_cash))
        self.model = model
        self.transport = transport or ExamClient(model=model, max_tokens=max_tokens)
        self.max_focus = max_focus
        self.short_locates = bool(short_locates)
        self.ledger_path = Path(ledger_path) if ledger_path else None
        self.broker = HistoricalSimBroker(set(self.symbols), initial_cash=self.initial_cash)
        self.calls: list[ExamCall] = []
        self.warnings: list[dict] = []
        self._last_marks = {}

    # ------------------------------------------------------------- prompt/data
    def _snapshot(self, day, asof):
        return get_v2_snapshot(day, asof, self.symbols, mode=self.mode, db_path=self.db_path)

    def _mark_prices(self):
        return dict(self._last_marks)

    def _account(self, cutoff):
        return self.broker.snapshot(cutoff, self._mark_prices())

    def _groups(self):
        try:
            import yaml
            data = yaml.safe_load((ROOT / "config/watchlist.yaml").read_text(encoding="utf-8"))
            return {symbol: group["name"] for group in data["groups"] for symbol in group["symbols"]}
        except Exception:  # noqa: BLE001
            return {}

    def _news_and_facts(self, snapshot, symbols):
        news = {}
        by_symbol = {}
        for item in snapshot.get("evidence", []):
            relevant = item.get("symbols") or []
            for symbol in relevant:
                if symbol not in symbols:
                    continue
                bucket = by_symbol.setdefault(symbol, [])
                if len(bucket) >= 2:
                    continue
                bucket.append({
                    "id": item.get("id"), "title": str(item.get("title") or "")[:180],
                    "kind": item.get("kind"), "first_published": item.get("first_published"),
                    "historical_status": item.get("historical_status", "UNKNOWN"),
                })
        news.update(by_symbol)
        facts = {}
        for item in snapshot.get("fundamentals", []):
            symbol = item.get("symbol")
            if symbol not in symbols:
                continue
            bucket = facts.setdefault(symbol, [])
            if len(bucket) >= 2:
                continue
            bucket.append({
                "id": "fact:" + symbol + ":" + str(item.get("tag")) + ":" + str(item.get("period_end")),
                "tag": item.get("tag"), "value": item.get("value"), "unit": item.get("unit"),
                "available_at": item.get("available_at"), "historical_status": item.get("historical_status"),
            })
        return news, facts

    def _scan_payload(self, snapshot, account, groups):
        news, facts = self._news_and_facts(snapshot, self.symbols)
        daily_by = {}
        pre_by = {}
        for bar in snapshot["bars"]:
            target = daily_by if bar["timeframe"] == "1Day" else pre_by
            target.setdefault(bar["symbol"], []).append(bar)
        universe = []
        for symbol in self.symbols:
            daily = daily_by.get(symbol, [])[-5:]
            pre = pre_by.get(symbol, [])[-1:]
            row = {
                "symbol": symbol, "group": groups.get(symbol, ""),
                "position": int(account.get("positions", {}).get(symbol, 0)),
                "daily": [_bar(item) for item in daily],
                "premarket": [_bar(item) for item in pre],
                "summary": _returns(daily_by.get(symbol, [])),
                "news": news.get(symbol, []), "facts": facts.get(symbol, []),
                "data_status": "OK" if daily else "UNAVAILABLE",
            }
            universe.append(row)
        qqq = daily_by.get("QQQ", [])[-10:]
        return {
            "exam": {
                "dataset_version": snapshot["dataset_version"], "mode": self.mode.value,
                "date": snapshot["date"], "as_of_utc": snapshot["asof_utc"],
                "feed": "IEX", "no_external_research": True, "current_memory_used": False,
                "historical_status": "EXPLORATORY_UNKNOWN_HISTORICAL_CONTENT_VERSION",
                "limitations": snapshot.get("limitations"),
            },
            "model_id": self.model, "as_of_utc": snapshot["asof_utc"],
            "account_snapshot_id": _sha(account),
            "account": account,
            "strategy": {
                "version": "exam-current-strategy",
                "entry_rules": "PULLBACK_RECLAIM or EVENT_MISPRICING with evidence; long or short only with invalidation.",
                "exit_rules": "AI may HOLD/REDUCE/CLOSE/COVER; DAY limit; no forced trade.",
            },
            "universe": universe,
            "market_reference": {
                "symbol": "QQQ", "description": "Nasdaq-100 ETF proxy; analysis only, not tradable",
                "daily": [_bar(item) for item in qqq],
            },
            "execution_contract": {
                "paper_simulation_only": True, "whole_shares": True, "order_type": "limit",
                "time_in_force": "day", "allow_long": True, "allow_short": True,
                "historical_short_locate_assumption": self.short_locate_assumption(),
                "stop_policy": "NONE only in simulator",
            },
        }

    def short_locate_assumption(self):
        return "ALL_SYMBOLS_ASSUMED_AVAILABLE_FOR_EXAM" if self.short_locates else "UNAVAILABLE"

    def _focus(self, snapshot, account, symbols):
        news, facts = self._news_and_facts(snapshot, symbols)
        daily_by, pre_by = {}, {}
        for bar in snapshot["bars"]:
            (daily_by if bar["timeframe"] == "1Day" else pre_by).setdefault(bar["symbol"], []).append(bar)
        rows = []
        for symbol in symbols:
            rows.append({
                "symbol": symbol, "position": int(account.get("positions", {}).get(symbol, 0)),
                "daily": [_bar(item) for item in daily_by.get(symbol, [])[-60:]],
                "premarket": [_bar(item) for item in pre_by.get(symbol, [])[-5:]],
                "news": news.get(symbol, []), "facts": facts.get(symbol, []),
            })
        return rows

    # ------------------------------------------------------------------- calls
    def _call(self, stage, system, user, parser):
        started = time.monotonic()
        last_error = None
        for attempt in range(2):
            try:
                response = self.transport(system, user)
                raw = response["raw"]
                parsed = parser(raw)
                call = ExamCall(
                    stage=stage, day="", system_sha256=_sha(system), user_sha256=_sha(user),
                    raw=raw, parsed=parsed.model_dump(mode="json"),
                    prompt_tokens=response.get("prompt_tokens"),
                    completion_tokens=response.get("completion_tokens"),
                    latency_ms=response.get("latency_ms", int((time.monotonic() - started) * 1000)),
                    status="OK",
                )
                self.calls.append(call)
                return parsed
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                self.calls.append(ExamCall(
                    stage=stage, day="", system_sha256=_sha(system), user_sha256=_sha(user),
                    raw="", parsed=None, prompt_tokens=None, completion_tokens=None,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    status="ERROR", error=str(exc)[:1000],
                ))
                if attempt == 0:
                    user = user + "\n\nYour prior answer failed validation. Return only the complete strict JSON object. Error: " + str(exc)[:800]
        raise ValueError(f"{stage} failed after retry: {last_error}")

    def _scan(self, snapshot, account):
        payload = self._scan_payload(snapshot, account, self._groups())
        system = EXAM_GUARD + (ROOT / "archive/legacy_prompts/universe_scan.txt").read_text(encoding="utf-8")
        result = self._call(
            "UNIVERSE_SCAN", system, _canon(snapshot["asof_utc"]) if False else _canon(payload),
            lambda raw: parse_scan(raw, symbols=self.symbols, now=_parse_time(snapshot["asof_utc"])),
        )
        expected_asof = _parse_time(snapshot["asof_utc"])
        if result.as_of_utc != expected_asof:
            self.warnings.append({"stage": "UNIVERSE_SCAN", "warning": "as_of_overridden",
                                  "actual": result.as_of_utc.isoformat(),
                                  "expected": expected_asof.isoformat()})
            result = result.model_copy(update={"as_of_utc": expected_asof})
        if result.account_snapshot_id != payload["account_snapshot_id"]:
            self.warnings.append({"stage": "UNIVERSE_SCAN", "warning": "account_snapshot_overridden",
                                  "actual": result.account_snapshot_id,
                                  "expected": payload["account_snapshot_id"]})
            result = result.model_copy(update={"account_snapshot_id": payload["account_snapshot_id"]})
        if result.model_id != self.model:
            self.warnings.append({"stage": "UNIVERSE_SCAN", "warning": "model_overridden",
                                  "actual": result.model_id, "expected": self.model})
            result = result.model_copy(update={"model_id": self.model})
        scan_ids = {e["id"] for row in payload["universe"] for e in row["news"] + row["facts"]}
        cleaned = []
        for candidate in result.candidates:
            unknown = [eid for eid in candidate.evidence_ids if eid not in scan_ids]
            if unknown:
                self.warnings.append({
                    "stage": "UNIVERSE_SCAN", "symbol": candidate.symbol,
                    "warning": "unknown_evidence_ids_removed", "ids": unknown,
                })
                candidate = candidate.model_copy(update={
                    "evidence_ids": [eid for eid in candidate.evidence_ids if eid in scan_ids],
                })
            cleaned.append(candidate)
        if cleaned != result.candidates:
            result = result.model_copy(update={"candidates": cleaned})
        return result, payload

    def _plan(self, scan, scan_payload, snapshot, account, focus_symbols):
        system = EXAM_GUARD + (ROOT / "archive/legacy_prompts/premarket_plan.txt").read_text(encoding="utf-8")
        focus = self._focus(snapshot, account, focus_symbols)
        payload = {
            "exam": scan_payload["exam"], "model_id": self.model,
            "as_of_utc": snapshot["asof_utc"],
            "account_snapshot_id": scan_payload["account_snapshot_id"],
            "account": account, "strategy": scan_payload["strategy"],
            "focus": focus,
            "scan_candidates": [candidate.model_dump(mode="json") for candidate in scan.candidates],
            "held_symbols": [s for s, q in account.get("positions", {}).items() if q],
            "stop_policy_choices": ["NONE"],
            "position_choices": ["OPEN_LONG", "CLOSE_LONG", "OPEN_SHORT", "COVER_SHORT", "HOLD", "WAIT"],
            "shorting": {"allowed": True, "locate_assumption": self.short_locate_assumption()},
            "execution_contract": scan_payload["execution_contract"],
            "unavailable_context": ["web_search", "live_quotes", "current_research_memory"],
        }
        result = self._call(
            "PREMARKET_PLAN", system, _canon(payload),
            lambda raw: parse_plan(raw, symbols=self.symbols, now=_parse_time(snapshot["asof_utc"])),
        )
        expected_asof = _parse_time(snapshot["asof_utc"])
        if result.as_of_utc != expected_asof:
            self.warnings.append({"stage": "PREMARKET_PLAN", "warning": "as_of_overridden",
                                  "actual": result.as_of_utc.isoformat(),
                                  "expected": expected_asof.isoformat()})
            result = result.model_copy(update={"as_of_utc": expected_asof})
        if result.account_snapshot_id != payload["account_snapshot_id"]:
            self.warnings.append({"stage": "PREMARKET_PLAN", "warning": "account_snapshot_overridden",
                                  "actual": result.account_snapshot_id,
                                  "expected": payload["account_snapshot_id"]})
            result = result.model_copy(update={"account_snapshot_id": payload["account_snapshot_id"]})
        if result.model_id != self.model:
            self.warnings.append({"stage": "PREMARKET_PLAN", "warning": "model_overridden",
                                  "actual": result.model_id, "expected": self.model})
            result = result.model_copy(update={"model_id": self.model})
        return result, payload

    # --------------------------------------------------------------- execution
    def _focus_symbols(self, scan, account):
        symbols = []
        for candidate in scan.candidates:
            if candidate.symbol not in symbols:
                symbols.append(candidate.symbol)
        for symbol, qty in account.get("positions", {}).items():
            if qty and symbol not in symbols:
                symbols.append(symbol)
        return symbols[: self.max_focus]

    def _regular_bars(self, day):
        start = datetime.combine(day, dtime(9, 30), ET).astimezone(timezone.utc)
        end = datetime.combine(day, dtime(16, 0), ET).astimezone(timezone.utc)
        with ExamStore(self.db_path, read_only=True) as store:
            rows = store.db.execute(
                "SELECT symbol,timeframe,ts,end_at,open,high,low,close,volume,feed FROM bars "
                "WHERE timeframe='1Min' AND feed='iex' AND day=? AND ts>=? AND ts<? "
                "ORDER BY ts,symbol", [day, start, end],
            ).fetchall()
        cols = ["symbol", "timeframe", "ts", "end_at", "open", "high", "low", "close", "volume", "feed"]
        return [dict(zip(cols, row)) for row in rows]

    def _daily_close(self, day):
        with ExamStore(self.db_path, read_only=True) as store:
            rows = store.db.execute(
                "SELECT symbol,close,ts FROM bars WHERE timeframe='1Day' AND feed='iex' "
                "AND day=? ORDER BY symbol", [day],
            ).fetchall()
        return {symbol: float(close) for symbol, close, _ in rows}

    def _submit_plan(self, plan, day):
        if plan.market_decision is not MarketDecision.TRADE:
            return []
        submitted = []
        cutoff = datetime.combine(day, dtime(11, 59, 29), ET)
        valid_until = plan.as_of_utc + timedelta(hours=6)
        for candidate in plan.candidates:
            if candidate.order_intent is None:
                continue
            intent = candidate.order_intent
            if intent.stop_policy.value == "BROKER_STOP":
                submitted.append({"symbol": candidate.symbol, "status": "rejected",
                                  "reason": "BROKER_STOP unsupported in exam simulator"})
                continue
            execution_triggers = [t for t in candidate.triggers if t.purpose.value == "EXECUTE_ORDER"]
            if not execution_triggers:
                submitted.append({"symbol": candidate.symbol, "status": "no_trigger"})
                continue
            action = intent.action.value
            order = self.broker.submit(
                symbol=candidate.symbol, action=action, qty=intent.qty,
                limit_price=Decimal(str(intent.limit_price)), submitted_at=cutoff,
                order_id=f"{day}-{candidate.plan_id}",
            )
            submitted.append({
                "symbol": candidate.symbol, "action": action, "qty": intent.qty,
                "limit_price": str(intent.limit_price), "order_id": order.id,
                "status": "submitted",
            })
        return submitted

    def _replay_orders(self, plan, day):
        bars = self._regular_bars(day)
        if plan.market_decision is not MarketDecision.TRADE:
            return [], bars
        cutoff = datetime.combine(day, dtime(11, 59, 29), ET)
        orders = {}
        for candidate in plan.candidates:
            intent = candidate.order_intent
            if intent is None or intent.stop_policy.value == "BROKER_STOP":
                continue
            triggers = [t for t in candidate.triggers if t.purpose.value == "EXECUTE_ORDER"]
            if not triggers:
                continue
            orders[candidate.symbol] = {"candidate": candidate, "intent": intent,
                                        "triggers": triggers, "submitted": False}
        submitted = []
        for bar in bars:
            stamp = bar["ts"].astimezone(ET)
            if stamp > cutoff:
                break
            fields = {"last": float(bar["close"]), "close": float(bar["close"])}
            for symbol, item in orders.items():
                if item["submitted"] or symbol != bar["symbol"]:
                    continue
                if any(self._conditions(trigger.all_of, fields) for trigger in item["triggers"]):
                    intent = item["intent"]
                    try:
                        order = self.broker.submit(
                            symbol=symbol, action=intent.action.value, qty=intent.qty,
                            limit_price=Decimal(str(intent.limit_price)), submitted_at=bar["ts"],
                            order_id=f"{day}-{item['candidate'].plan_id}",
                        )
                        item["submitted"] = True
                        submitted.append({"symbol": symbol, "action": intent.action.value,
                                           "qty": intent.qty, "limit_price": str(intent.limit_price),
                                           "order_id": order.id, "submitted_at": bar["ts"].isoformat()})
                    except Exception as exc:  # noqa: BLE001
                        submitted.append({"symbol": symbol, "status": "rejected",
                                          "reason": str(exc)[:300]})
                        item["submitted"] = True
            for bar2 in [bar]:
                self.broker.process_bar(bar2)
        return submitted, bars

    @staticmethod
    def _conditions(conditions, fields):
        if not conditions:
            return False
        for condition in conditions:
            field = condition.field.value
            if field not in fields:
                return False
            actual = fields[field]
            target = float(condition.value)
            op = condition.op.value
            if op == "<=" and not actual <= target:
                return False
            if op == "<" and not actual < target:
                return False
            if op == ">=" and not actual >= target:
                return False
            if op == ">" and not actual > target:
                return False
            if op == "==" and not actual == target:
                return False
        return True

    def _trade_ledger(self, before_fills):
        # Build FIFO closed trades from all fills.  The simulator emits fills
        # with action, signed position changes and fees.
        lots = {}
        trades = []
        for fill in self.broker.fills:
            action = fill.action
            symbol = fill.symbol
            direction = "short" if action == "OPEN_SHORT" else "long"
            if action in {"OPEN_LONG", "BUY"}:
                lots.setdefault(symbol, []).append({
                    "qty": fill.qty, "price": fill.price, "fee": fill.fee, "at": fill.at,
                    "direction": "long", "order": fill.order_id,
                })
            elif action == "OPEN_SHORT":
                lots.setdefault(symbol, []).append({
                    "qty": fill.qty, "price": fill.price, "fee": fill.fee, "at": fill.at,
                    "direction": "short", "order": fill.order_id,
                })
            elif action in {"CLOSE_LONG", "REDUCE", "SELL", "COVER_SHORT"}:
                remaining = fill.qty
                while remaining and lots.get(symbol):
                    lot = lots[symbol][0]
                    qty = min(remaining, lot["qty"])
                    gross = (fill.price - lot["price"]) * qty if lot["direction"] == "long" \
                        else (lot["price"] - fill.price) * qty
                    entry_fee = lot["fee"] * Decimal(qty) / Decimal(lot["qty"])
                    close_fee = fill.fee * Decimal(qty) / Decimal(fill.qty)
                    trades.append({
                        "symbol": symbol, "direction": lot["direction"], "qty": qty,
                        "entry_price": float(lot["price"]), "exit_price": float(fill.price),
                        "entry_at": lot["at"].isoformat(), "exit_at": fill.at.isoformat(),
                        "pnl": float(gross - entry_fee - close_fee),
                        "fees": float(entry_fee + close_fee), "holding_minutes":
                            (fill.at - lot["at"]).total_seconds() / 60,
                        "entry_order": lot["order"], "exit_order": fill.order_id,
                    })
                    lot["qty"] -= qty
                    lot["fee"] -= entry_fee
                    remaining -= qty
                    if lot["qty"] <= 0:
                        lots[symbol].pop(0)
        return trades

    # ------------------------------------------------------------------- run
    def run(self, days):
        result = ReplayResult(start_cash=float(self.initial_cash),
                              short_locate_assumption=self.short_locate_assumption())
        for day_value in days:
            day = date.fromisoformat(str(day_value))
            cutoff = datetime.combine(day, dtime(9, 0), ET)
            if self.short_locates:
                for symbol in self.symbols:
                    self.broker.short_locates[(day, symbol)] = True
            # Marks at 09:00 use only the prior completed daily closes.
            prior = self._daily_close(day - timedelta(days=1))
            if not prior:
                with ExamStore(self.db_path, read_only=True) as store:
                    rows = store.db.execute(
                        "SELECT symbol,close FROM bars WHERE timeframe='1Day' AND feed='iex' "
                        "AND day<? ORDER BY day DESC LIMIT 48", [day],
                    ).fetchall()
                prior = {symbol: float(close) for symbol, close in rows}
            self._last_marks = prior
            account = self._account(cutoff)
            snapshot = self._snapshot(day, cutoff)
            scan, scan_payload = self._scan(snapshot, account)
            focus = self._focus_symbols(scan, account)
            plan, plan_payload = self._plan(scan, scan_payload, snapshot, account, focus)
            submitted, bars = self._replay_orders(plan, day)
            close_marks = self._daily_close(day)
            close_at = datetime.combine(day, dtime(16, 0), ET)
            self.broker.expire_day(day)
            borrow = self.broker.accrue_borrow(day, close_marks)
            state = self.broker.snapshot(close_at, close_marks)
            gross = sum(abs(q) * close_marks.get(s, 0.0) for s, q in self.broker.positions.items())
            result.days.append({
                "day": day.isoformat(), "scan_candidates": [c.symbol for c in scan.candidates],
                "focus": focus, "market_decision": plan.market_decision.value,
                "submitted": submitted, "fills": len(self.broker.fills),
                "cash": float(self.broker.cash), "equity": float(state["equity"]),
                "gross_exposure": gross, "borrow_cost": float(borrow),
                "positions": dict(self.broker.positions),
            })
            result.equity_curve.append(float(state["equity"]))
            result.decisions.append({
                "day": day.isoformat(), "scan": scan.model_dump(mode="json"),
                "plan": plan.model_dump(mode="json"), "submitted": submitted,
            })
            self._last_marks = close_marks
        result.trades = self._trade_ledger(0)
        result.fills = [
            {"order_id": fill.order_id, "symbol": fill.symbol, "action": fill.action,
             "qty": fill.qty, "price": float(fill.price), "fee": float(fill.fee),
             "at": fill.at.isoformat()}
            for fill in self.broker.fills
        ]
        result.open_positions = self._open_positions()
        result.simulator = self.broker
        result.calls = [call.__dict__ for call in self.calls]
        # QQQ buy-and-hold: first open, last close, from the same frozen bars.
        if days:
            first = date.fromisoformat(str(days[0]))
            last = date.fromisoformat(str(days[-1]))
            with ExamStore(self.db_path, read_only=True) as store:
                first_rows = store.db.execute(
                    "SELECT open FROM bars WHERE symbol='QQQ' AND timeframe='1Min' AND feed='iex' "
                    "AND day=? ORDER BY ts LIMIT 1", [first],
                ).fetchall()
                last_rows = store.db.execute(
                    "SELECT close FROM bars WHERE symbol='QQQ' AND timeframe='1Min' AND feed='iex' "
                    "AND day=? ORDER BY ts DESC LIMIT 1", [last],
                ).fetchall()
            if first_rows and last_rows:
                result.qqq_prices = [float(first_rows[0][0]), float(last_rows[0][0])]
        return result

    def run_saved(self, days, decisions):
        """Rebuild execution/equity from saved plans without any AI call."""
        result = ReplayResult(start_cash=float(self.initial_cash),
                              short_locate_assumption=self.short_locate_assumption())
        by_day = {item["day"]: item for item in decisions}
        for day_value in days:
            day = date.fromisoformat(str(day_value))
            if self.short_locates:
                for symbol in self.symbols:
                    self.broker.short_locates[(day, symbol)] = True
            cutoff = datetime.combine(day, dtime(9, 0), ET)
            self._last_marks = self._daily_close(day - timedelta(days=1))
            if not self._last_marks:
                with ExamStore(self.db_path, read_only=True) as store:
                    rows = store.db.execute(
                        "SELECT symbol,close FROM bars WHERE timeframe='1Day' AND feed='iex' "
                        "AND day<? ORDER BY day DESC LIMIT 48", [day],
                    ).fetchall()
                self._last_marks = {symbol: float(close) for symbol, close in rows}
            account = self._account(cutoff)
            plan = PremarketPlan.model_validate(by_day[day.isoformat()]["plan"])
            submitted, bars = self._replay_orders(plan, day)
            close_marks = self._daily_close(day)
            close_at = datetime.combine(day, dtime(16, 0), ET)
            self.broker.expire_day(day)
            borrow = self.broker.accrue_borrow(day, close_marks)
            state = self.broker.snapshot(close_at, close_marks)
            gross = sum(abs(q) * close_marks.get(s, 0.0) for s, q in self.broker.positions.items())
            result.days.append({
                "day": day.isoformat(), "market_decision": plan.market_decision.value,
                "submitted": submitted, "fills": len(self.broker.fills),
                "cash": float(self.broker.cash), "equity": float(state["equity"]),
                "gross_exposure": gross, "borrow_cost": float(borrow),
                "positions": dict(self.broker.positions),
            })
            result.equity_curve.append(float(state["equity"]))
            result.decisions.append(by_day[day.isoformat()])
            self._last_marks = close_marks
        result.trades = self._trade_ledger(0)
        result.fills = [
            {"order_id": fill.order_id, "symbol": fill.symbol, "action": fill.action,
             "qty": fill.qty, "price": float(fill.price), "fee": float(fill.fee),
             "at": fill.at.isoformat()}
            for fill in self.broker.fills
        ]
        result.open_positions = self._open_positions()
        result.simulator = self.broker
        if days:
            first = date.fromisoformat(str(days[0])); last = date.fromisoformat(str(days[-1]))
            with ExamStore(self.db_path, read_only=True) as store:
                first_rows = store.db.execute(
                    "SELECT open FROM bars WHERE symbol='QQQ' AND timeframe='1Min' AND feed='iex' "
                    "AND day=? ORDER BY ts LIMIT 1", [first],
                ).fetchall()
                last_rows = store.db.execute(
                    "SELECT close FROM bars WHERE symbol='QQQ' AND timeframe='1Min' AND feed='iex' "
                    "AND day=? ORDER BY ts DESC LIMIT 1", [last],
                ).fetchall()
            if first_rows and last_rows:
                result.qqq_prices = [float(first_rows[0][0]), float(last_rows[0][0])]
        return result

    def _open_positions(self):
        def entry_for(symbol, direction):
            items = [
                fill for fill in self.broker.fills
                if fill.symbol == symbol and (
                    fill.action == "OPEN_SHORT" if direction == "short"
                    else fill.action in {"OPEN_LONG", "BUY"}
                )
            ]
            qty = sum(fill.qty for fill in items)
            if not qty:
                return None
            return sum(float(fill.price) * fill.qty for fill in items) / qty
        out = []
        for symbol, qty in self.broker.positions.items():
            if not qty:
                continue
            mark = float(self._last_marks.get(symbol, 0))
            direction = "long" if qty > 0 else "short"
            entry = entry_for(symbol, direction)
            pnl = None
            if entry is not None:
                pnl = ((mark - entry) * qty) if qty > 0 else ((entry - mark) * abs(qty))
            out.append({
                "symbol": symbol, "qty": qty, "direction": direction,
                "entry_price": entry, "mark": mark,
                "market_value": float(Decimal(str(qty)) * Decimal(str(mark))),
                "unrealized_pnl": pnl,
            })
        return out

    def report(self, result):
        metrics = result.metrics()
        return {
            "mode": "EXAM_REPLAY",
            "dataset": str(self.db_path),
            "exam_mode": self.mode.value,
            "model": self.model,
            "days": [day["day"] for day in result.days],
            "in_window": False,
            "short_locate_assumption": result.short_locate_assumption,
            "metrics": metrics,
            "equity_curve": result.equity_curve,
            "trades": result.trades,
            "fills": result.fills,
            "open_positions": result.open_positions,
            "daily": result.days,
            "decisions": result.decisions,
            "calls": result.calls,
            "warnings": self.warnings,
            "no_real_orders": True,
            "no_production_memory": True,
            "win_rate_note": (
                "No closed trades in this replay; win rate/payoff are not defined. "
                "Open positions are marked at the final close and shown separately."
                if not result.trades else ""
            ),
        }


def write_report(report, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path
