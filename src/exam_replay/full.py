"""Full single-agent historical exam replay.

The AI contract is the existing production contract:
  * UNIVERSE_SCAN
  * PREMARKET_PLAN
  * INTRADAY_RECHECK
  * POSITION_REVIEW (existing production prompt/schema, used for stop review)

Python performs the 09:30-12:00 trigger loop, calls the AI only on
REQUEST_AI_RECHECK, and applies the returned instruction through the isolated
broker.  After 12:00 ET it never calls the AI; already-active limit and stop
orders continue to be replayed against completed IEX minute bars.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from exam_v2.evaluation import evaluate
from exam_v2.snapshot import ExamMode

from trading_bot.decision_schema import (
    MarketDecision, PremarketPlan, RecheckDecision, TriggerPurpose,
    parse_plan, parse_position_review, parse_recheck, parse_scan,
    PositionAction,
)
from trading_bot.deepseek_client import DeepSeek

from .broker import ExamBroker
from .runner import ExamReplay, ROOT, _canon, _parse_time, _sha
from .parsing import normalize_plan, normalize_stop_metadata

EXAM_MODE = ExamMode.EXPLORATORY
SUBMIT_CUTOFF = dtime(11, 59, 30)
LOCAL_END = dtime(12, 0, 0)


@dataclass
class RuntimePlan:
    candidate: object
    status: str = "ACTIVE"
    fired: set[str] = field(default_factory=set)
    order_ids: list[str] = field(default_factory=list)
    submitted: bool = False


class FullExamReplay(ExamReplay):
    def __init__(
        self, *, symbols, api_key, db_path=None, model="deepseek-flash",
        max_tokens=7000, max_focus=4, initial_cash=100000,
        output_dir=None, audit_hook=None, ai_client=None,
    ):
        super().__init__(
            symbols=symbols, db_path=db_path or (ROOT / "data/exam_v2.duckdb"),
            mode=EXAM_MODE, initial_cash=initial_cash, model=model,
            transport=None, max_focus=max_focus, max_tokens=max_tokens,
            short_locates=True,
        )
        self.broker = ExamBroker(
            self.symbols, initial_cash=Decimal(str(initial_cash)),
            short_locates=True,
        )
        self.ai = ai_client or DeepSeek(
            api_key, model=model, max_tokens=max_tokens, temperature=0.2,
            repair_attempts=1, input_record=self._ai_input,
            audit=self._ai_audit, record=self._ai_record,
        )
        self.ai_calls: list[dict] = []
        self.ai_inputs: list[dict] = []
        self.ai_raw: list[dict] = []
        self.audit_hook = audit_hook
        self.output_dir = Path(output_dir or (ROOT / "data/exam_replay_runs/full15"))
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.execution_contract = {
            "paper_simulation_only": True,
            "allow_long": True,
            "allow_short": True,
            "allow_shorting": True,
            "shorting": {"enabled": True, "shorting_enabled": True},
            "allow_broker_stop": True,
            "broker_stop_verified": True,
            "broker_stop_status": "VERIFIED",
            "allow_adjust_stop": True,
            "allow_remove_stop": True,
            "short_locate_assumption": "ALL_SYMBOLS_ASSUMED_AVAILABLE_FOR_EXAM",
            "new_order_window_et": "09:30-11:59:30",
            "local_ai_window_et": "09:30-11:59:59",
            "after_1200": "broker order replay only; no new AI decisions",
            "time_in_force": {"entry": "DAY", "protective_stop": "GTC"},
        }
        self._day = None
        self._last_recheck_at = None
        self._rechecks_today = 0
        self._position_reviewed = False
        self.daily_audit: list[dict] = []
        self._runtime: list[RuntimePlan] = []
        self._equity_marks = []

    # ---------------------------------------------------------------- AI audit
    def _ai_input(self, record):
        self.ai_inputs.append(record)
        if self.audit_hook:
            self.audit_hook({"kind": "ai_input", **record})

    def _ai_audit(self, kind, record):
        if kind == "ai_raw":
            self.ai_raw.append(record)
        if self.audit_hook:
            self.audit_hook({"kind": kind, **record})

    def _ai_record(self, record):
        self.ai_calls.append(record)
        if self.audit_hook:
            self.audit_hook({"kind": "ai_call", **record})

    # -------------------------------------------------------------- premarket
    def _premarket_payload(self, snapshot, account):
        groups = self._groups()
        base = self._scan_payload(snapshot, account, groups)
        base["exam"] = {
            "dataset_version": snapshot["dataset_version"], "mode": self.mode.value,
            "date": snapshot["date"], "as_of_utc": snapshot["asof_utc"],
            "feed": "IEX", "no_external_research": True, "current_memory_used": False,
            "historical_status": "EXPLORATORY_UNKNOWN_HISTORICAL_CONTENT_VERSION",
            "execution_contract": self.execution_contract,
        }
        for row in base["universe"]:
            row["shortability"] = {"shortable": True, "easy_to_borrow": True,
                                    "assumption": self.execution_contract["short_locate_assumption"]}
        return base

    def _scan(self, snapshot, account):
        payload = self._premarket_payload(snapshot, account)
        parsed, meta = self.ai.complete(
            "UNIVERSE_SCAN", payload,
            lambda raw: parse_scan(normalize_stop_metadata(raw)[0], symbols=self.symbols, now=_parse_time(snapshot["asof_utc"])),
            include_raw=True,
        )
        parsed = self._validate_metadata("SCAN", parsed, payload, snapshot)
        scan_ids = {e["id"] for row in payload["universe"] for e in row["news"] + row["facts"]}
        cleaned = []
        for candidate in parsed.candidates:
            unknown = [eid for eid in candidate.evidence_ids if eid not in scan_ids]
            if unknown:
                self.warnings.append({"stage": "UNIVERSE_SCAN", "symbol": candidate.symbol,
                                      "warning": "unknown_evidence_ids_removed", "ids": unknown})
                candidate = candidate.model_copy(update={
                    "evidence_ids": [eid for eid in candidate.evidence_ids if eid in scan_ids],
                })
            cleaned.append(candidate)
        if cleaned != parsed.candidates:
            parsed = parsed.model_copy(update={"candidates": cleaned})
        return parsed, payload, meta

    def _plan(self, scan, scan_payload, snapshot, account, focus_symbols):
        focus = self._focus(snapshot, account, focus_symbols)
        payload = {
            "exam": scan_payload["exam"], "model_id": self.model,
            "as_of_utc": snapshot["asof_utc"],
            "account_snapshot_id": scan_payload["account_snapshot_id"],
            "account": account, "strategy": scan_payload["strategy"],
            "focus": focus,
            "scan_candidates": [c.model_dump(mode="json") for c in scan.candidates],
            "held_symbols": [s for s, q in account.get("positions", {}).items() if q],
            "stop_policy_choices": ["NONE", "BROKER_STOP"],
            "position_choices": ["OPEN_LONG", "CLOSE_LONG", "OPEN_SHORT", "COVER_SHORT", "HOLD", "WAIT"],
            "shorting": {"allowed": True, "locate_assumption": self.short_locate_assumption()},
            "execution_contract": self.execution_contract,
            "unavailable_context": ["web_search", "live_quotes", "current_research_memory"],
        }
        parsed, meta = self.ai.complete(
            "PREMARKET_PLAN", payload,
            lambda raw: parse_plan(normalize_plan(normalize_stop_metadata(raw)[0])[0], symbols=self.symbols, now=_parse_time(snapshot["asof_utc"])),
            include_raw=True,
        )
        parsed = self._validate_metadata("PLAN", parsed, payload, snapshot)
        return parsed, payload, meta

    def _validate_metadata(self, stage, parsed, payload, snapshot):
        expected = _parse_time(snapshot["asof_utc"])
        if parsed.as_of_utc != expected:
            self.warnings.append({"stage": stage, "warning": "as_of_overridden",
                                  "actual": parsed.as_of_utc.isoformat(),
                                  "expected": expected.isoformat()})
            parsed = parsed.model_copy(update={"as_of_utc": expected})
        if parsed.account_snapshot_id != payload["account_snapshot_id"]:
            self.warnings.append({"stage": stage, "warning": "account_overridden"})
            parsed = parsed.model_copy(update={"account_snapshot_id": payload["account_snapshot_id"]})
        if parsed.model_id != self.model:
            self.warnings.append({"stage": stage, "warning": "model_overridden"})
            parsed = parsed.model_copy(update={"model_id": self.model})
        return parsed

    # ------------------------------------------------------------------ helpers
    def _account_context(self, account):
        return {
            "account_snapshot_id": account.get("account_snapshot_id"),
            "cash": account.get("cash"), "equity": account.get("equity"),
            "buying_power": account.get("buying_power"),
            "available_non_margin_cash": account.get("available_non_margin_cash"),
            "reserved_buy_cash": account.get("reserved_buy_cash"),
            "long_market_value": account.get("long_market_value"),
            "short_market_value": account.get("short_market_value"),
            "gross_exposure": account.get("gross_exposure"),
            "unrealized_pnl": account.get("unrealized_pnl"),
            "realized_pnl": account.get("realized_pnl"),
            "fees_paid": account.get("fees_paid"),
            "borrow_cost": account.get("borrow_cost"),
            "shorting_enabled": True, "allow_shorting": True,
            "positions": account.get("positions_detail", []),
            "open_orders": account.get("open_orders", []),
            "stops": account.get("stops", []),
            "short_locate_assumption": account.get("short_locate_assumption"),
            "asof": account.get("asof_utc"),
        }

    def _quote(self, bar):
        return {
            "symbol": bar["symbol"], "last": float(bar["close"]),
            "ts": bar["ts"].isoformat(), "end_at": bar["end_at"].isoformat(),
            "bid": None, "ask": None, "spread": None,
        }

    def _history(self, snapshot, symbol):
        daily = [b for b in snapshot["bars"] if b["symbol"] == symbol and b["timeframe"] == "1Day"][-60:]
        pre = [b for b in snapshot["bars"] if b["symbol"] == symbol and b["timeframe"] == "1Min"][-20:]
        week = [b for b in snapshot["weekly_bars"] if b["symbol"] == symbol][-13:]
        month = [b for b in snapshot["monthly_bars"] if b["symbol"] == symbol][-6:]

        def compact(items):
            out = []
            for b in items:
                stamp = b.get("ts") or b.get("period")
                if hasattr(stamp, "astimezone"):
                    stamp = stamp.astimezone(timezone.utc).isoformat()
                else:
                    stamp = str(stamp)
                out.append({
                    "ts": stamp, "end_at": stamp,
                    "open": float(b["open"]), "high": float(b["high"]),
                    "low": float(b["low"]), "close": float(b["close"]),
                    "volume": int(b.get("volume") or 0),
                })
            return out
        return {"daily_recent": compact(daily), "premarket_recent": compact(pre),
                "weekly": compact(week), "monthly": compact(month)}

    def _fields(self, bar):
        return {"last": float(bar["close"]), "close": float(bar["close"])}

    @staticmethod
    def _conditions(conditions, fields):
        if not conditions:
            return False
        for condition in conditions:
            field = condition.field.value
            if field not in fields or fields[field] is None:
                return False
            actual = fields[field]
            target = float(condition.value)
            op = condition.op.value
            if op == "<=" and not actual <= target: return False
            if op == "<" and not actual < target: return False
            if op == ">=" and not actual >= target: return False
            if op == ">" and not actual > target: return False
            if op == "==" and not actual == target: return False
        return True

    def _cancel_plan_orders(self, runtime, at, reason):
        for order_id in runtime.order_ids:
            order = self.broker.orders.get(order_id)
            if order and order.status in {"NEW", "PARTIALLY_FILLED"}:
                self.broker.cancel(order_id, at=at, reason=reason)

    def _submit_candidate(self, candidate, at, plan_id, purpose=None):
        intent = candidate.order_intent
        if intent is None:
            return None
        stop_policy = intent.stop_policy.value
        stop_loss = intent.stop_loss.stop_price if intent.stop_loss else None
        purpose = purpose or ("ENTRY" if intent.action.value in {"OPEN_LONG", "OPEN_SHORT", "BUY"} else "EXIT")
        order = self.broker.submit(
            symbol=intent.symbol, action=intent.action.value, qty=intent.qty,
            limit_price=intent.limit_price, submitted_at=at,
            plan_id=plan_id or candidate.plan_id, purpose=purpose,
            reason=candidate.analysis[:300], time_in_force="DAY",
            stop_policy=stop_policy, stop_loss=stop_loss,
        )
        return order

    def _invalidate(self, runtime, at, reason):
        runtime.status = "INVALIDATED"
        self._cancel_plan_orders(runtime, at, "plan invalidated")
        self.daily_audit[-1]["events"].append({"at": at.isoformat(), "kind": "plan_invalidated",
                                               "plan_id": runtime.candidate.plan_id,
                                               "reason": reason})

    # --------------------------------------------------------------- rechecks
    def _recheck(self, runtime, trigger, bar, snapshot, account):
        candidate = runtime.candidate
        at = bar["end_at"]
        if self._rechecks_today >= 20:
            self.daily_audit[-1]["events"].append({"at": at.isoformat(), "kind": "recheck_suppressed",
                                                   "reason": "daily_limit"})
            return False
        if self._last_recheck_at and (at - self._last_recheck_at).total_seconds() < 120:
            self.daily_audit[-1]["events"].append({"at": at.isoformat(), "kind": "recheck_suppressed",
                                                   "reason": "cooldown"})
            return False
        self._last_recheck_at = at
        self._rechecks_today += 1
        quote = self._quote(bar)
        payload = {
            "exam": self._exam_meta(snapshot), "as_of_utc": at.isoformat(),
            "model_id": self.model, "account_snapshot_id": account.get("account_snapshot_id"),
            "plan": candidate.model_dump(mode="json"),
            "plan_id": candidate.plan_id,
            "trigger": trigger.model_dump(mode="json"),
            "trigger_fields": self._fields(bar),
            "quote": quote,
            "account": self._account_context(account),
            "position": next((p for p in account.get("positions_detail", [])
                              if p["symbol"] == candidate.symbol), None),
            "history": self._history(snapshot, candidate.symbol),
            "deadline_utc": datetime.combine(self._day, LOCAL_END).replace(
                tzinfo=__import__("zoneinfo").ZoneInfo("America/New_York")).isoformat(),
            "rechecks_used_today": self._rechecks_today,
            "risk_notes": {
                "stop": next((s for s in account.get("stops", [])
                              if s["symbol"] == candidate.symbol), None),
                "gross_exposure": account.get("gross_exposure"),
                "buying_power": account.get("buying_power"),
                "short_locate_assumption": self.execution_contract["short_locate_assumption"],
            },
        }
        try:
            parsed, meta = self.ai.complete(
                "INTRADAY_RECHECK", payload,
                lambda raw: parse_recheck(normalize_stop_metadata(raw)[0], plan_id=candidate.plan_id, now=at),
                include_raw=True,
            )
        except Exception as exc:  # noqa: BLE001
            self.daily_audit[-1]["events"].append({"at": at.isoformat(), "kind": "recheck_error",
                                                   "plan_id": candidate.plan_id,
                                                   "error": str(exc)[:500]})
            return False
        self.daily_audit[-1]["events"].append({
            "at": at.isoformat(), "kind": "recheck", "plan_id": candidate.plan_id,
            "trigger_id": trigger.trigger_id, "decision": parsed.decision.value,
            "reason": parsed.reason, "tokens": (meta.get("prompt_tokens") or 0) +
            (meta.get("completion_tokens") or 0),
        })
        if parsed.decision in {RecheckDecision.INVALIDATE, RecheckDecision.NO_TRADE}:
            self._invalidate(runtime, at, "recheck: " + parsed.decision.value)
            return True
        if parsed.decision is RecheckDecision.CONTINUE:
            return True
        if parsed.decision is RecheckDecision.REPLACE:
            if not parsed.order_intent:
                self.daily_audit[-1]["events"].append({"at": at.isoformat(),
                                                       "kind": "recheck_replace_rejected",
                                                       "reason": "missing order_intent"})
                return True
            intent = parsed.order_intent
            if intent.symbol != candidate.symbol:
                return True
            try:
                self._cancel_plan_orders(runtime, at, "recheck replace")
                order = self.broker.submit(
                    symbol=intent.symbol, action=intent.action.value, qty=intent.qty,
                    limit_price=intent.limit_price, submitted_at=at,
                    plan_id=candidate.plan_id + "-replace",
                    reason=parsed.reason[:300], time_in_force="DAY",
                    stop_policy=intent.stop_policy.value,
                    stop_loss=intent.stop_loss.stop_price if intent.stop_loss else None,
                )
                runtime.order_ids.append(order.id)
                runtime.status = "SUPERSEDED"
                self.daily_audit[-1]["events"].append({
                    "at": at.isoformat(), "kind": "recheck_replace_order",
                    "order_id": order.id, "action": intent.action.value, "qty": intent.qty,
                })
            except Exception as exc:  # noqa: BLE001
                self.daily_audit[-1]["events"].append({"at": at.isoformat(),
                                                       "kind": "recheck_replace_rejected",
                                                       "reason": str(exc)[:300]})
            return True
        return False

    # ----------------------------------------------------------- position review
    def _position_review(self, snapshot, account, bar):
        positions = account.get("positions_detail", [])
        if not positions:
            return
        payload = {
            "exam": self._exam_meta(snapshot), "as_of_utc": bar["end_at"].isoformat(),
            "model_id": self.model,
            "account": self._account_context(account),
            "positions": positions,
            "open_orders": account.get("open_orders", []),
            "stops": account.get("stops", []),
            "quote": self._quote(bar),
            "execution_contract": self.execution_contract,
            "stop_choices": ["HOLD", "ADJUST_STOP", "REMOVE_STOP", "NO_STOP"],
        }
        try:
            parsed, meta = self.ai.complete(
                "POSITION_REVIEW", payload,
                lambda raw: parse_position_review(normalize_stop_metadata(raw)[0], symbols=self.symbols),
                include_raw=True,
            )
        except Exception as exc:  # noqa: BLE001
            self.daily_audit[-1]["events"].append({"at": bar["end_at"].isoformat(),
                                                   "kind": "position_review_error",
                                                   "error": str(exc)[:500]})
            return
        for instruction in parsed.instructions:
            at = bar["end_at"]
            self.daily_audit[-1]["events"].append({
                "at": at.isoformat(), "kind": "position_instruction",
                "symbol": instruction.symbol, "action": instruction.action.value,
                "reason": instruction.reason,
            })
            try:
                if instruction.action in {PositionAction.HOLD, PositionAction.NO_STOP}:
                    continue
                if instruction.action is PositionAction.ADJUST_STOP:
                    if instruction.stop_loss is None:
                        raise ValueError("ADJUST_STOP missing stop_loss")
                    self.broker.set_stop(
                        instruction.symbol, stop_price=instruction.stop_loss.stop_price,
                        at=at, reason=instruction.reason[:300],
                        plan_id=instruction.plan_id or "",
                    )
                    continue
                if instruction.action is PositionAction.REMOVE_STOP:
                    self.broker.remove_stop(
                        instruction.symbol, at=at, reason=instruction.reason[:300],
                        plan_id=instruction.plan_id or "",
                    )
                    continue
                action = instruction.action.value
                purpose = "ENTRY" if action in {"OPEN_LONG", "OPEN_SHORT", "BUY"} else "EXIT"
                order = self.broker.submit(
                    symbol=instruction.symbol, action=action, qty=instruction.qty,
                    limit_price=instruction.limit_price, submitted_at=at,
                    plan_id=instruction.plan_id or f"pos-review-{self._day.isoformat()}-{instruction.symbol}",
                    purpose=purpose, reason=instruction.reason[:300], time_in_force="DAY",
                    stop_policy=instruction.stop_policy.value,
                    stop_loss=instruction.stop_loss.stop_price if instruction.stop_loss else None,
                )
                self.daily_audit[-1]["events"].append({
                    "at": at.isoformat(), "kind": "position_order",
                    "order_id": order.id, "symbol": order.symbol,
                    "action": order.action, "qty": order.qty,
                })
            except Exception as exc:  # noqa: BLE001
                self.daily_audit[-1]["events"].append({
                    "at": at.isoformat(), "kind": "position_instruction_rejected",
                    "symbol": instruction.symbol, "error": str(exc)[:300],
                })

    # --------------------------------------------------------------------- run
    def _exam_meta(self, snapshot):
        return {
            "dataset_version": snapshot["dataset_version"], "mode": self.mode.value,
            "date": snapshot["date"], "as_of_utc": snapshot["asof_utc"],
            "feed": "IEX", "no_external_research": True, "current_memory_used": False,
            "historical_status": "EXPLORATORY_UNKNOWN_HISTORICAL_CONTENT_VERSION",
            "execution_contract": self.execution_contract,
        }

    def run(self, days):
        from .runner import ReplayResult
        result = ReplayResult(start_cash=float(self.initial_cash),
                              short_locate_assumption=self.short_locate_assumption())
        for day_value in days:
            day = date.fromisoformat(str(day_value))
            self._day = day
            cutoff = datetime.combine(day, dtime(9, 0)).replace(
                tzinfo=__import__("zoneinfo").ZoneInfo("America/New_York"))
            if self.short_locates:
                self.broker.short_locates = True
            self._last_marks = self._daily_close(day - timedelta(days=1))
            if not self._last_marks:
                from exam_data import ExamStore
                with ExamStore(self.db_path, read_only=True) as store:
                    rows = store.db.execute(
                        "SELECT symbol,close FROM bars WHERE timeframe='1Day' AND feed='iex' "
                        "AND day<? ORDER BY day DESC LIMIT 48", [day],
                    ).fetchall()
                self._last_marks = {s: float(c) for s, c in rows}
            account = self.broker.account(cutoff, self._last_marks)
            snapshot = self._snapshot(day, cutoff)
            daily = {"day": day.isoformat(), "events": [], "ai_calls": 0,
                     "scan_candidates": [], "focus": [], "market_decision": "NO_TRADE",
                     "plan": None}
            try:
                scan, scan_payload, _scan_meta = self._scan(snapshot, account)
                focus = self._focus_symbols(scan, account)
                plan, plan_payload, _plan_meta = self._plan(
                    scan, scan_payload, snapshot, account, focus)
            except Exception as exc:  # noqa: BLE001
                # Fail closed for the session, never fabricate a trade.  The
                # failed session remains in the record and still receives an
                # end-of-day equity mark.
                self.warnings.append({"stage": "PREMARKET", "day": day.isoformat(),
                                      "warning": "plan_failed_closed", "error": str(exc)[:800]})
                plan = PremarketPlan(
                    as_of_utc=snapshot["asof_utc"], model_id=self.model,
                    account_snapshot_id=account["account_snapshot_id"],
                    market_decision=MarketDecision.NO_TRADE, candidates=[],
                    run_id=f"exam-failed-{day.isoformat()}",
                )
                scan = type("Scan", (), {"candidates": []})()
                focus = []
                daily["events"].append({"at": cutoff.isoformat(), "kind": "plan_failed_closed",
                                        "error": str(exc)[:800]})
            self.daily_audit.append(daily)
            self.daily_audit[-1]["scan_candidates"] = [c.symbol for c in scan.candidates]
            self.daily_audit[-1]["focus"] = focus
            self.daily_audit[-1]["market_decision"] = plan.market_decision.value
            self.daily_audit[-1]["plan"] = plan.model_dump(mode="json")
            self._rechecks_today = 0
            self._last_recheck_at = None
            self._position_reviewed = False
            self._runtime = [RuntimePlan(candidate=c) for c in plan.candidates]
            bars = self._regular_bars(day)
            by_ts = {}
            for bar in bars:
                by_ts.setdefault(bar["end_at"], {})[bar["symbol"]] = bar
            for ts in sorted(by_ts):
                if ts.astimezone(__import__("zoneinfo").ZoneInfo("America/New_York")).time() > dtime(16, 0):
                    continue
                bars_here = by_ts[ts]
                for bar in bars_here.values():
                    self.broker.process_bar(bar)
                    self._last_marks[bar["symbol"]] = float(bar["close"])
                if ts.astimezone(__import__("zoneinfo").ZoneInfo("America/New_York")).time() >= LOCAL_END:
                    # At 12:00: broker orders only, no AI, no new local order.
                    continue
                if not self._position_reviewed:
                    self._position_reviewed = True
                    self._position_review(snapshot, self.broker.account(ts, self._last_marks),
                                          next(iter(bars_here.values())))
                # AI and trigger processing only before the submission cutoff.
                if ts.astimezone(__import__("zoneinfo").ZoneInfo("America/New_York")).time() >= SUBMIT_CUTOFF:
                    continue
                account_now = self.broker.account(ts, self._last_marks)
                for runtime in self._runtime:
                    candidate = runtime.candidate
                    if runtime.status != "ACTIVE":
                        continue
                    try:
                        valid_until = candidate.plan_valid_until_utc
                    except Exception:  # noqa: BLE001
                        self._invalidate(runtime, ts, "invalid valid_until")
                        continue
                    if valid_until <= ts:
                        runtime.status = "EXPIRED"
                        self._cancel_plan_orders(runtime, ts, "plan expired")
                        continue
                    bar = next((b for b in bars_here.values() if b["symbol"] == candidate.symbol), None)
                    if bar is None:
                        continue
                    fields = self._fields(bar)
                    if candidate.invalidation and self._conditions(candidate.invalidation, fields):
                        self._invalidate(runtime, ts, "invalidation condition")
                        continue
                    for trigger in candidate.triggers:
                        if trigger.trigger_id in runtime.fired:
                            continue
                        if not self._conditions(trigger.all_of, fields):
                            continue
                        if trigger.purpose is TriggerPurpose.INVALIDATE_PLAN:
                            runtime.fired.add(trigger.trigger_id)
                            self._invalidate(runtime, ts, "trigger: " + trigger.trigger_id)
                            break
                        if trigger.purpose is TriggerPurpose.REQUEST_AI_RECHECK:
                            if self._recheck(runtime, trigger, bar, self._snapshot(day, ts), account_now):
                                runtime.fired.add(trigger.trigger_id)
                            break
                        if trigger.purpose is TriggerPurpose.EXECUTE_ORDER:
                            if candidate.order_intent is None or runtime.submitted:
                                runtime.fired.add(trigger.trigger_id)
                                continue
                            try:
                                order = self._submit_candidate(candidate, ts, candidate.plan_id)
                                runtime.order_ids.append(order.id)
                                runtime.submitted = True
                                runtime.fired.add(trigger.trigger_id)
                                self.daily_audit[-1]["events"].append({
                                    "at": ts.isoformat(), "kind": "order_submitted",
                                    "order_id": order.id, "symbol": order.symbol,
                                    "action": order.action, "qty": order.qty,
                                    "limit": str(order.limit_price),
                                })
                            except Exception as exc:  # noqa: BLE001
                                runtime.fired.add(trigger.trigger_id)
                                self.daily_audit[-1]["events"].append({
                                    "at": ts.isoformat(), "kind": "order_rejected",
                                    "symbol": candidate.symbol, "reason": str(exc)[:300],
                                })
                            break
            # 12:00-16:00 already processed by the same loop; no AI.
            self.broker.expire_day(day)
            close_marks = self._daily_close(day)
            if not close_marks:
                close_marks = dict(self._last_marks)
            self._last_marks = close_marks
            borrow = self.broker.accrue_borrow(day, close_marks)
            close_at = datetime.combine(day, dtime(16, 0)).replace(
                tzinfo=__import__("zoneinfo").ZoneInfo("America/New_York"))
            state = self.broker.account(close_at, close_marks)
            gross = state["gross_exposure"]
            submitted_here = [
                e for e in self.daily_audit[-1]["events"]
                if e.get("kind") in {"order_submitted", "position_order", "recheck_replace_order"}
            ]
            result.days.append({
                "day": day.isoformat(), "scan_candidates": [c.symbol for c in scan.candidates],
                "focus": focus, "market_decision": plan.market_decision.value,
                "submitted": submitted_here, "fills": len(self.broker.fills),
                "cash": state["cash"], "equity": state["equity"],
                "gross_exposure": gross, "borrow_cost": float(borrow),
                "positions": dict(self.broker.positions),
                "equity_curve": state["equity"],
                "realized_pnl": state["realized_pnl"],
                "unrealized_pnl": state["unrealized_pnl"],
                "fees_paid": state["fees_paid"],
            })
            result.equity_curve.append(float(state["equity"]))
            result.decisions.append(self.daily_audit[-1])
            self._last_marks = close_marks
        result.trades = [t.__dict__ | {
            "entry_price": float(t.entry_price), "exit_price": float(t.exit_price),
            "pnl": float(t.pnl), "fees": float(t.fees),
            "entry_at": t.entry_at.isoformat(), "exit_at": t.exit_at.isoformat(),
            "holding_minutes": (t.exit_at - t.entry_at).total_seconds() / 60,
            "direction": t.direction,
        } for t in self.broker.closed_trades]
        result.fills = [f.__dict__ | {
            "price": float(f.price), "fee": float(f.fee),
            "at": f.at.isoformat(),
            "trigger_price": float(f.trigger_price) if f.trigger_price is not None else None,
            "slippage": float(f.slippage),
        } for f in self.broker.fills]
        result.open_positions = self.broker.open_positions(self._last_marks)
        result.simulator = None
        result.calls = self.ai_calls
        if days:
            first = date.fromisoformat(str(days[0])); last = date.fromisoformat(str(days[-1]))
            from exam_data import ExamStore
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

    def report(self, result):
        if len(result.equity_curve) < 2:
            metrics = {
                "total_return": 0.0, "max_drawdown": 0.0, "sharpe_252": None,
                "trades": len(result.trades), "win_rate": None, "payoff_ratio": None,
                "capital_utilization_mean": 0.0, "average_holding_minutes": None,
                "long": {"trades": 0, "pnl": 0.0, "win_rate": None},
                "short": {"trades": 0, "pnl": 0.0, "win_rate": None},
                "qqq_buy_hold_return": None,
            }
        else:
            metrics = evaluate(
                result.equity_curve,
                [{**t, "holding_minutes": (datetime.fromisoformat(t["exit_at"]) - datetime.fromisoformat(t["entry_at"])).total_seconds() / 60}
                 for t in result.trades],
                [d["gross_exposure"] for d in result.days],
                qqq_prices=result.qqq_prices or None,
            )
        qqq_daily = self._qqq_daily(result.days)
        exposure_matched = 1.0
        for i in range(1, len(result.days)):
            if i >= len(qqq_daily):
                break
            if qqq_daily[i] is None or qqq_daily[i - 1] is None:
                continue
            previous = result.days[i - 1]
            ratio = (previous["gross_exposure"] / previous["equity"]) if previous["equity"] else 0.0
            qqq_return = (qqq_daily[i] / qqq_daily[i - 1] - 1) if qqq_daily[i - 1] else 0.0
            exposure_matched *= 1.0 + ratio * qqq_return
        metrics["exposure_matched_qqq_return"] = exposure_matched - 1.0
        metrics["qqq_daily"] = qqq_daily
        metrics["slippage_cost"] = sum(
            f.get("slippage", 0.0) * f.get("qty", 0) for f in result.fills
        )
        metrics["fees_paid"] = sum(f.get("fee", 0.0) for f in result.fills)
        metrics["borrow_cost"] = sum(d.get("borrow_cost", 0.0) for d in result.days)
        metrics["stop_trigger_count"] = sum(
            1 for f in result.fills if f.get("reason") == "STOP_TRIGGERED"
        )
        metrics["recheck_count"] = sum(
            1 for d in result.days for e in d.get("events", []) if e.get("kind") == "recheck"
        )
        metrics["recheck_decisions"] = [
            {"day": d["day"], "decision": e.get("decision"), "reason": e.get("reason")}
            for d in result.days for e in d.get("events", []) if e.get("kind") == "recheck"
        ]
        metrics["no_trade_days"] = [
            d["day"] for d in result.days
            if not any(e.get("kind") in {"order_submitted", "position_order", "recheck_replace_order"}
                       for e in d.get("events", []))
        ]
        metrics["cash_ratio_mean"] = sum(
            1.0 - (d["gross_exposure"] / d["equity"] if d["equity"] else 0.0)
            for d in result.days
        ) / len(result.days) if result.days else None
        state = self.broker.account(
            datetime.combine(self._day, dtime(16, 0)).replace(
                tzinfo=__import__("zoneinfo").ZoneInfo("America/New_York")),
            self._last_marks,
        )
        return {
            "mode": "EXAM_FULL", "dataset": str(self.db_path),
            "exam_mode": self.mode.value, "model": self.model,
            "days": [d["day"] for d in result.days],
            "metrics": metrics,
            "equity_curve": result.equity_curve,
            "trades": result.trades, "fills": result.fills,
            "open_positions": result.open_positions,
            "daily": result.days,
            "decisions": result.decisions,
            "calls": self.ai_calls,
            "ai_inputs": self.ai_inputs,
            "ai_raw": self.ai_raw,
            "warnings": self.warnings,
            "execution_contract": self.execution_contract,
            "account": state,
            "no_real_orders": True,
            "no_production_memory": True,
            "win_rate_note": (
                "No closed trades in this replay; win rate/payoff are not defined."
                if not result.trades else ""
            ),
        }

    def _qqq_daily(self, days):
        from exam_data import ExamStore
        dates = [date.fromisoformat(d["day"]) for d in days]
        with ExamStore(self.db_path, read_only=True) as store:
            rows = store.db.execute(
                "SELECT day, close FROM bars WHERE symbol='QQQ' AND timeframe='1Day' AND feed='iex' "
                "AND day>=? AND day<=? ORDER BY day", [dates[0], dates[-1]],
            ).fetchall() if dates else []
        values = {str(d): float(c) for d, c in rows}
        return [values.get(str(d), None) for d in dates]
