"""Session engine: premarket plan -> deterministic triggers -> explicit AI recheck.

The AI owns strategy; Python owns the clock, data provenance, plan validity,
trigger evaluation, order legality and reconciliation.  The engine never
re-computes a target weight and never changes an AI quantity.
"""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_FLOOR

from .decision_schema import (
    MarketDecision,
    PremarketPlan,
    RecheckDecision,
    SchemaError,
    as_intent,
    parse_plan,
    parse_recheck,
    parse_scan,
)
from .execution import OrderService
from .market_data import (
    SessionPhase,
    compact_history,
    daily_history,
    quote_fields,
    session_info,
)
from .risk_manager import (
    BrokerStopUnsupported,
    RiskRejected,
    reserved_buy_cash,
    stop_orders,
    validate_new_intent,
)
from .settings import load_config


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_iso(value) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)


def _hash(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]


def _clamp_text(value, limit=4000):
    return (value or "")[:limit]


class Engine:
    def __init__(
        self,
        store,
        broker,
        ai,
        config,
        data,
        daily_data=None,
        clock=None,
        mode="paper",
        research=None,
        require_watchdog=None,
        check_disk_config=None,
    ):
        self.store = store
        self.broker = broker
        self.ai = ai
        self.config = config
        self.symbols = tuple(config.symbols)
        self.policy = config.policy
        self.risk = config.risk
        self.execution = config.execution
        self.data = data
        self.daily_data = daily_data
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.mode = mode
        self.research = research
        self.require_watchdog = (mode == "paper") if require_watchdog is None else require_watchdog
        self.check_disk_config = (mode == "paper") if check_disk_config is None else check_disk_config
        self.orders = OrderService(store, broker, self.execution, clock=self.clock)
        self.started_at = self.clock()
        self._last_account_event = None
        self._last_scan = None
        self._last_config_error = None
        if store.active_config() is None:
            from .settings import config_snapshot

            store.set_active_config(config_snapshot(config))
        recovered = store.recover_stale_leases()
        if recovered:
            store.event("order_recovery", {"count": len(recovered), "states": "NOT_SENT",
                                            "reason": "stale local lease before dispatch"})

    # -------------------------------------------------------------- lifecycle
    def tick(self):
        now = self.clock()
        try:
            self.store.heartbeat("worker", ok=True, detail={"phase": "tick"})
            session = session_info(now, self.execution)
            if session.phase is SessionPhase.NO_SESSION:
                return "closed"
            if self.check_disk_config and self._config_pending():
                return "config_pending"
            if not self.policy.ready() or not self.symbols:
                return "disabled"
            if session.premarket:
                return self.premarket(session)
            if session.active:
                return self.active(session)
            return self.after_close(session)
        except Exception as exc:  # noqa: BLE001 - a tick must not kill the strategy
            try:
                self.store.error("engine_tick", exc, outcome="cycle_failed")
            finally:
                return "failed"

    def _config_pending(self) -> bool:
        try:
            disk = load_config()
        except Exception as exc:  # noqa: BLE001
            if self._last_config_error != str(exc):
                self._last_config_error = str(exc)
                self.store.error("config_load", exc, outcome="config_pending")
            self.store.set_config_pending(True)
            return True
        active = self.store.active_config() or {}
        active_hash = (active.get("hashes") or {}).get("combined")
        if not active_hash:
            self.store.set_active_config({
                "symbols": list(self.symbols), "hashes": dict(self.config.hashes),
                "version": self.policy.version,
                "execution": self.execution.model_dump(),
                "policy": {"version": self.policy.version, "enabled": self.policy.enabled},
            })
            return False
        if active_hash != disk.hash:
            if not self.store.config_pending():
                self.store.event("config_pending", {
                    "active": active_hash, "disk": disk.hash,
                    "outcome": "new risk frozen until safe restart",
                })
            self.store.set_config_pending(True)
            return True
        if self.store.config_pending():
            self.store.set_config_pending(False)
        return False

    def _watchdog_ok(self, now) -> bool:
        if not self.require_watchdog:
            return True
        health = self.store.health("watchdog", self.execution.watchdog_stale_seconds)
        if health["consecutive_failures"] >= 3:
            reason = "WATCHDOG_UNHEALTHY: repeated reconciliation failures"
            if self.store.safety_reason() != reason:
                self.store.block(reason)
            return False
        if health["alive"] and not health["stale"]:
            last_success = _parse_iso(health["last_success"]) if health["last_success"] else None
            if last_success is None:
                if (now - self.started_at).total_seconds() > 90:
                    reason = "WATCHDOG_UNHEALTHY: no successful reconciliation"
                    if self.store.safety_reason() != reason:
                        self.store.block(reason)
                return False
            if (now - last_success).total_seconds() > max(
                120, 3 * self.execution.watchdog_stale_seconds
            ):
                reason = "WATCHDOG_UNHEALTHY: stale last successful reconciliation"
                if self.store.safety_reason() != reason:
                    self.store.block(reason)
                return False
            return True
        if (now - self.started_at).total_seconds() > 90:
            reason = "WATCHDOG_UNHEALTHY: no recent reconciliation heartbeat"
            if self.store.safety_reason() != reason:
                self.store.block(reason)
        return False

    def _account(self, now):
        account = self.broker.snapshot()
        if self._last_account_event is None or (now - self._last_account_event).total_seconds() >= 60:
            self.store.event("account", account)
            self._last_account_event = now
        return account

    def _account_context(self, account):
        held = {symbol: qty for symbol, qty in account.get("quantities", {}).items() if float(qty) > 0}
        stops = stop_orders(account)
        stop_by_symbol = {}
        for order in stops:
            stop_by_symbol.setdefault(str(order.get("symbol")), []).append({
                "id": order.get("id"),
                "client_order_id": order.get("client_order_id"),
                "qty": order.get("qty"),
                "filled_qty": order.get("filled_qty"),
                "stop_price": order.get("stop_price"),
                "status": order.get("status"),
                "time_in_force": order.get("time_in_force"),
            })
        positions = []
        for symbol, qty in held.items():
            detail = (account.get("position_details") or {}).get(symbol, {})
            positions.append({
                "symbol": symbol,
                "qty": float(qty),
                "whole_shares": int(Decimal(str(qty)).to_integral_value(rounding=ROUND_FLOOR)),
                "market_value": float(detail.get("market_value") or 0),
                "avg_entry_price": float(detail.get("avg_entry_price") or 0),
                "unrealized_pl": float(detail.get("unrealized_pl") or 0),
                "current_price": float(detail.get("current_price") or 0),
                "available_qty": float((account.get("available_quantities") or {}).get(symbol, qty)),
                "broker_stops": stop_by_symbol.get(symbol, []),
                "stop_status": self._stop_status(symbol, stop_by_symbol.get(symbol, [])),
            })
        reserved = reserved_buy_cash(account, self.store.unresolved_orders())
        cash = float(account.get("cash") or 0)
        context = {
            "account_snapshot_id": _hash({
                "asof": account.get("asof"), "equity": account.get("equity"),
                "cash": account.get("cash"),
                "positions": account.get("quantities"), "orders": [
                    (o.get("client_order_id"), o.get("status"), o.get("filled_qty"))
                    for o in account.get("open_orders", [])
                ],
                "config": self.config.hash,
            }),
            "equity": float(account.get("equity") or 0),
            "last_equity": float(account.get("last_equity") or 0),
            "cash": cash,
            "buying_power": float(account.get("buying_power") or 0),
            "non_margin_cash": max(0.0, cash),
            "reserved_buy_cash": reserved,
            "available_non_margin_cash": max(0.0, cash - reserved),
            "blocked": bool(account.get("blocked")),
            "positions": positions,
            "open_orders": [
                {k: order.get(k) for k in (
                    "id", "client_order_id", "symbol", "side", "qty", "filled_qty",
                    "type", "time_in_force", "limit_price", "stop_price", "status",
                    "order_class", "legs", "created_at",
                ) if k in order}
                for order in account.get("open_orders", [])
            ],
            "stops": stops,
            "asof": account.get("asof"),
            "account_id_hash": account.get("account_id_hash"),
        }
        return context, held

    def _stop_status(self, symbol, stops):
        if stops:
            return "VERIFIED"
        intent = self.store.latest_buy_intent(symbol)
        if not intent:
            return "UNKNOWN"
        policy = str(intent.get("stop_policy") or "NONE").upper()
        if policy == "NONE":
            return "NONE_BY_AI"
        return "FAILED"

    # --------------------------------------------------------------- premarket
    def premarket(self, session):
        key = f"{session.trading_day}:premarket"
        if not self.store.claim_stage(
            key, session.trading_day, "PREMARKET",
            stale_seconds=max(60, self.execution.premarket_retry_seconds),
            max_attempts=5,
        ):
            return "premarket_not_claimed"
        try:
            self.orders.reconcile()
            now = self.clock()
            account = self._account(now)
            account_ctx, held = self._account_context(account)
            history, failures, unavailable = self._collect_universe(session)
            self.store.event("premarket_data", {
                "trading_day": session.trading_day,
                "available": len(history), "failures": failures[:20],
                "unavailable": unavailable[:20],
            })
            if not history:
                self.store.finish_stage(key, "FAILED", {"reason": "no usable history"})
                self.store.event("premarket_aborted", {"reason": "no usable history"})
                return "premarket_failed"
            scan = self._universe_scan(session, account_ctx, history, held, unavailable)
            if scan is None:
                self.store.finish_stage(key, "FAILED", {"reason": "universe scan failed"})
                return "premarket_failed"
            focus = self._focus_symbols(scan, held, account, history)
            unavailable_held = [symbol for symbol in held if symbol not in history]
            if unavailable_held:
                self.store.event("held_data_unavailable", {
                    "trading_day": session.trading_day,
                    "symbols": unavailable_held,
                    "outcome": "no new trade for these symbols; no fabricated data",
                })
            if not focus:
                self.store.finish_stage(key, "DONE", {"reason": "no candidates or holdings"})
                self.store.event("premarket_no_trade", {"trading_day": session.trading_day})
                return "premarket_no_trade"
            plan, meta = self._premarket_plan(
                session, account_ctx, history, scan, focus, held, unavailable_held
            )
            if plan is None:
                self.store.finish_stage(key, "FAILED", {"reason": "premarket plan failed"})
                return "premarket_failed"
            if not session_info(self.clock(), self.execution).premarket:
                self.store.finish_stage(key, "FAILED", {"reason": "plan completed after premarket"})
                self.store.event("premarket_aborted", {"reason": "plan completed after 09:30 ET"})
                return "premarket_failed"
            saved, blocked = self._save_plans(session, plan, focus, held)
            self.store.finish_stage(key, "DONE", {
                "plan_candidates": len(plan.candidates), "saved": saved, "blocked": blocked,
                "scan_candidates": len(scan.candidates),
            })
            return "premarket_planned" if saved else "premarket_no_trade"
        except Exception as exc:  # noqa: BLE001
            self.store.error("premarket", exc, cycle=key, outcome="premarket_failed")
            try:
                self.store.finish_stage(key, "FAILED", {"error": type(exc).__name__})
            except Exception:  # noqa: BLE001
                pass
            return "premarket_failed"

    def _collect_universe(self, session):
        now = self.clock()
        symbols = list(self.symbols)
        history = {}
        failures = []
        unavailable = []
        if not self.daily_data:
            for symbol in symbols:
                unavailable.append({"symbol": symbol, "reason": "daily_history_unavailable"})
            self.store.event("premarket_data", {"failures": [], "unavailable": unavailable})
            return history, failures, unavailable
        workers = max(1, min(int(self.execution.data_workers), len(symbols), 8))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="premarket-data") as pool:
            futures = {pool.submit(self._daily_for, symbol, now): symbol for symbol in symbols}
            for future in futures:
                symbol = futures[future]
                try:
                    daily = future.result()
                    if daily is None or daily.empty:
                        unavailable.append({"symbol": symbol, "reason": "no_daily_history"})
                    else:
                        history[symbol] = daily
                except Exception as exc:  # noqa: BLE001
                    failures.append({"symbol": symbol, "type": type(exc).__name__, "reason": str(exc)[:500]})
                    unavailable.append({"symbol": symbol, "reason": "daily_history_invalid"})
        return history, failures, unavailable

    def _daily_for(self, symbol, now):
        return daily_history(self.daily_data(symbol), now)

    def _intraday_for(self, symbol):
        if not self.data:
            return None
        frame = self.data(symbol)
        if frame is None or frame.empty:
            return None
        if getattr(frame.index, "tz", None) is None:
            raise ValueError("Missing intraday timezone")
        frame = frame.sort_index()
        keep = [c for c in ("open", "high", "low", "close", "volume") if c in frame.columns]
        if len(keep) != 5:
            raise ValueError("Missing OHLCV")
        return frame[keep].tail(100)

    def _quote_map(self, symbols):
        quotes = {}
        errors = []
        unique = list(dict.fromkeys(symbols))
        workers = max(1, min(int(self.execution.data_workers), len(unique), 8))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="quote") as pool:
            futures = {pool.submit(self.broker.quote, symbol, self.risk, None): symbol for symbol in unique}
            for future in futures:
                symbol = futures[future]
                try:
                    quotes[symbol] = future.result()
                except Exception as exc:  # noqa: BLE001
                    errors.append({"symbol": symbol, "type": type(exc).__name__, "reason": str(exc)[:300]})
        if errors:
            self.store.event("quote_errors", {"errors": errors})
        return quotes

    def _trim_research(self):
        if not getattr(self, "research", None):
            return {}, {"status": "UNAVAILABLE", "reason": "research memory not configured"}
        try:
            return self.research.summaries(self.symbols), self.research.capabilities()
        except Exception as exc:  # noqa: BLE001
            self.store.error("research", exc, outcome="research_unavailable")
            return {}, {"status": "UNAVAILABLE", "reason": type(exc).__name__}

    def _trim_news(self):
        if not getattr(self, "research", None):
            return {}
        try:
            return self.research.latest_news(self.symbols, limit=6)
        except Exception as exc:  # noqa: BLE001
            self.store.error("research_news", exc, outcome="news_unavailable")
            return {}

    def _base_payload(self, session, account_ctx):
        research, capabilities = self._trim_research()
        news = self._trim_news()
        return {
            "as_of_utc": _iso(self.clock()),
            "model_id": getattr(self.ai, "model", "unknown"),
            "account_snapshot_id": account_ctx["account_snapshot_id"],
            "session": {
                "phase": session.phase.value,
                "now_et": _iso(session.now_et),
                "market_open": _iso(session.market_open) if session.market_open else None,
                "active_end": _iso(session.active_end) if session.active_end else None,
                "submit_cutoff": _iso(session.submit_cutoff) if session.submit_cutoff else None,
            },
            "strategy": {
                "version": self.policy.version,
                "entry_rules": self.policy.entry_rules,
                "exit_rules": self.policy.exit_rules,
                "analysis_rules": self.policy.analysis_rules,
            },
            "account": account_ctx,
            "research": research,
            "news": news,
            "research_capabilities": capabilities,
            "execution_contract": {
                "mode": "Paper only",
                "order_type": "limit",
                "time_in_force": "day",
                "whole_shares": True,
                "extended_hours": False,
                "no_margin": True,
                "no_short": True,
                "broker_stop_allowed": bool(
                    self.execution.allow_broker_stop and self.execution.broker_stop_verified
                ),
                "broker_stop_status": (
                    "VERIFIED" if self.execution.allow_broker_stop and self.execution.broker_stop_verified
                    else "UNVERIFIED"
                ),
                "non_positional_protections": {
                    "max_daily_loss": self.risk.max_daily_loss,
                    "max_daily_orders": self.risk.max_daily_orders,
                },
            },
            "unavailable_context": (
                ["sector_performance", "premarket_change", "all_market_breadth"]
                if news else
                ["news", "sector_performance", "premarket_change", "all_market_breadth"]
            ),
            "data_provenance": {
                "feed": "IEX",
                "volume_scope": "IEX only, not consolidated market volume",
            },
        }

    def _universe_scan(self, session, account_ctx, history, held, unavailable):
        payload = self._base_payload(session, account_ctx)
        universe = []
        for symbol, daily in history.items():
            compact = compact_history(daily, None)
            # The full-pool scan is deliberately lighter than a deep plan.
            compact["daily_recent"] = compact["daily_recent"][-30:]
            compact["weekly"] = compact["weekly"][-13:]
            compact["monthly"] = compact["monthly"][-6:]
            universe.append({"symbol": symbol, "history": compact, "data_status": "OK"})
        payload["universe"] = universe
        payload["unavailable_symbols"] = unavailable
        payload["held_symbols"] = sorted(held)
        payload["scan_scope"] = "fixed watchlist only"
        try:
            scan, meta = self.ai.complete(
                "UNIVERSE_SCAN",
                payload,
                lambda raw: parse_scan(raw, symbols=self.symbols, now=self.clock()),
            )
        except Exception as exc:  # noqa: BLE001
            self.store.error("universe_scan", exc, outcome="scan_failed")
            return None
        if scan.account_snapshot_id != account_ctx["account_snapshot_id"]:
            self.store.error("universe_scan", ValueError("account_snapshot_id mismatch"),
                             outcome="scan_rejected")
            return None
        if abs((_parse_iso(scan.as_of_utc) - self.clock()).total_seconds()) > 900:
            self.store.error("universe_scan", ValueError("scan as_of_utc is stale"),
                             outcome="scan_rejected")
            return None
        self.store.save_daily_scan(
            session.trading_day, "OK",
            [candidate.model_dump(mode="json") for candidate in scan.candidates],
            _hash([candidate.model_dump(mode="json") for candidate in scan.candidates]),
        )
        self.store.event("universe_scan", {
            "trading_day": session.trading_day,
            "candidates": [candidate.symbol for candidate in scan.candidates],
            "skipped": scan.skipped_symbols,
            **meta,
        })
        return scan

    def _focus_symbols(self, scan, held, account, history):
        symbols = []
        for candidate in scan.candidates:
            if candidate.symbol in history and candidate.symbol not in symbols:
                symbols.append(candidate.symbol)
        for symbol in held:
            if symbol in history and symbol not in symbols:
                symbols.append(symbol)
        for order in account.get("open_orders", []):
            symbol = str(order.get("symbol") or "")
            if symbol in self.symbols and symbol in history and symbol not in symbols:
                symbols.append(symbol)
        return symbols[:24]

    def _premarket_plan(self, session, account_ctx, history, scan, focus, held, unavailable_held):
        payload = self._base_payload(session, account_ctx)
        focus_rows = []
        quotes = self._quote_map(focus)
        for symbol in focus:
            daily = history.get(symbol)
            if daily is None:
                continue
            try:
                intraday = self._intraday_for(symbol)
            except Exception as exc:  # noqa: BLE001
                intraday = None
                self.store.event("premarket_data", {
                    "symbol": symbol, "stage": "intraday", "reason": str(exc)[:300],
                })
            focus_rows.append({
                "symbol": symbol,
                "held": symbol in held,
                "history": compact_history(daily, intraday),
                "quote": quotes.get(symbol),
            })
        payload["focus"] = focus_rows
        payload["scan_candidates"] = [candidate.model_dump(mode="json") for candidate in scan.candidates]
        payload["held_symbols"] = sorted(held)
        payload["held_data_unavailable"] = sorted(unavailable_held)
        payload["stop_policy_choices"] = ["NONE", "BROKER_STOP"]
        try:
            plan, meta = self.ai.complete(
                "PREMARKET_PLAN",
                payload,
                lambda raw: parse_plan(raw, symbols=self.symbols, now=self.clock()),
            )
        except Exception as exc:  # noqa: BLE001
            self.store.error("premarket_plan", exc, outcome="plan_failed")
            return None, {}
        if abs((_parse_iso(plan.as_of_utc) - self.clock()).total_seconds()) > 900:
            self.store.error("premarket_plan", ValueError("plan as_of_utc is stale"), outcome="plan_rejected")
            return None, meta
        if plan.account_snapshot_id != account_ctx["account_snapshot_id"]:
            self.store.error("premarket_plan", ValueError("account_snapshot_id mismatch"),
                             outcome="plan_rejected")
            return None, meta
        meta = {**meta, "market_decision": plan.market_decision.value,
                "candidates": len(plan.candidates)}
        self.store.event("premarket_plan", {
            "trading_day": session.trading_day,
            "market_decision": plan.market_decision.value,
            "candidates": [candidate.symbol for candidate in plan.candidates],
            **meta,
        })
        return plan, meta

    def _save_plans(self, session, plan: PremarketPlan, focus, held):
        if plan.market_decision is MarketDecision.NO_TRADE:
            return 0, 0
        saved = blocked = 0
        focus_set = set(focus)
        planned_symbols = {candidate.symbol for candidate in plan.candidates}
        missing_positions = [
            symbol for symbol in held
            if symbol in focus_set and symbol not in planned_symbols
        ]
        if missing_positions:
            self.store.event("position_review_missing", {
                "trading_day": session.trading_day,
                "symbols": missing_positions,
                "outcome": "treated as HOLD; no automatic exit",
            })
        self.store.supersede_plans(session.trading_day, reason="new premarket plan")
        for candidate in plan.candidates:
            if candidate.veto:
                continue
            if candidate.symbol not in focus_set:
                self.store.event("plan_rejected", {
                    "plan_id": candidate.plan_id,
                    "reason": "candidate was not in the supplied focus/data set",
                })
                blocked += 1
                continue
            if candidate.plan_valid_until_utc > self._active_end_utc(session):
                self.store.event("plan_rejected", {
                    "plan_id": candidate.plan_id, "reason": "plan_valid_until beyond active window",
                })
                blocked += 1
                continue
            if candidate.order_intent and candidate.order_intent.stop_policy.value == "BROKER_STOP" and not (
                self.execution.allow_broker_stop and self.execution.broker_stop_verified
            ):
                self.store.event("plan_rejected", {
                    "plan_id": candidate.plan_id, "reason": "BROKER_STOP_UNSUPPORTED",
                })
                blocked += 1
                continue
            if not candidate.triggers and candidate.order_intent is None:
                continue
            candidate_dict = candidate.model_dump(mode="json")
            # Plan ids are namespaced by trading day.  The AI may reuse a
            # friendly id such as "plan-1" on another day; without the prefix
            # SQLite would keep the old trading_day and the new plan would be
            # invisible to active_plans(new_day).
            scoped_plan_id = f"{session.trading_day}:{candidate.plan_id}"
            candidate_dict["plan_id"] = scoped_plan_id
            plan_payload = {
                "plan_id": scoped_plan_id,
                "mode": "PREMARKET_PLAN",
                "trading_day": session.trading_day,
                "plan_valid_until_utc": candidate.plan_valid_until_utc.isoformat(),
                "candidate": candidate_dict,
                "candidates": [candidate_dict],
                "market_decision": plan.market_decision.value,
                "account_snapshot_id": plan.account_snapshot_id,
                "as_of_utc": plan.as_of_utc.isoformat(),
            }
            self.store.save_plan(
                plan_payload, config_hash=self.config.hash,
                trading_day=session.trading_day, supersede=False,
            )
            saved += 1
        if saved == 0:
            self.store.event("premarket_no_trade", {
                "trading_day": session.trading_day, "blocked": blocked,
            })
        return saved, blocked

    @staticmethod
    def _active_end_utc(session):
        return session.active_end.astimezone(timezone.utc) if session.active_end else None

    # ------------------------------------------------------------------ active
    def active(self, session):
        now = self.clock()
        if not session.can_submit:
            return "after_cutoff"
        if self.store.config_pending():
            return "config_pending"
        if self.store.paused():
            return "paused"
        if not self._watchdog_ok(now):
            return "watchdog_unhealthy"
        freeze = self.store.freeze_reason()
        if freeze:
            return "frozen"
        plans = self.store.active_plans(session.trading_day)
        if not plans:
            return "active_no_plan"
        if self._last_scan is not None and (now - self._last_scan).total_seconds() < self.execution.trigger_poll_seconds:
            return "active_wait"
        self._last_scan = now
        symbols = []
        for plan in plans:
            candidate = (plan.get("payload") or {}).get("candidate") or {}
            if candidate and candidate.get("symbol"):
                symbols.append(candidate["symbol"])
        try:
            account = self._account(now)
        except Exception as exc:  # noqa: BLE001
            self.store.error("active_snapshot", exc, outcome="active_frozen")
            return "active_snapshot_failed"
        quotes = self._quote_map(symbols)
        account_ctx, held = self._account_context(account)
        buy_attempts = self.store.count_buy_attempts(session.trading_day)
        triggered = 0
        for plan in plans:
            plan_id = plan["plan_id"]
            if plan.get("config_hash") != self.config.hash:
                self.store.set_plan_status(plan_id, "INVALIDATED", "config changed")
                self.store.event("plan_invalidated", {
                    "plan_id": plan_id, "reason": "config_hash_mismatch",
                })
                continue
            payload = plan.get("payload") or {}
            candidate = payload.get("candidate") or {}
            if not candidate:
                continue
            try:
                valid_until = _parse_iso(payload.get("plan_valid_until_utc") or plan.get("valid_until"))
            except Exception:  # noqa: BLE001
                self.store.set_plan_status(plan_id, "INVALID", "invalid plan_valid_until")
                continue
            if valid_until <= now:
                self.store.set_plan_status(plan_id, "EXPIRED", "plan_valid_until")
                continue
            symbol = candidate.get("symbol")
            quote = quotes.get(symbol)
            fields = quote_fields(quote) if quote else {}
            invalidation = candidate.get("invalidation") or []
            if invalidation and self._conditions_true(invalidation, fields):
                self.store.set_plan_status(plan_id, "INVALIDATED", "invalidation condition")
                continue
            for trigger in candidate.get("triggers") or []:
                if not self._conditions_true(trigger.get("all_of") or [], fields):
                    continue
                purpose = trigger.get("purpose")
                trigger_id = trigger.get("trigger_id")
                fired = self.store.trigger_fired(plan_id, trigger_id)
                if fired:
                    continue
                if purpose == "INVALIDATE_PLAN":
                    self.store.mark_trigger_fired(plan_id, trigger_id, _hash(fields))
                    self.store.set_plan_status(plan_id, "INVALIDATED", "trigger: " + trigger_id)
                    self.store.event("plan_invalidated", {"plan_id": plan_id, "trigger_id": trigger_id,
                                                          "fields": fields})
                    break
                if purpose == "REQUEST_AI_RECHECK":
                    if self._recheck_budget_exhausted(session.trading_day):
                        self.store.event("recheck_suppressed", {"plan_id": plan_id, "reason": "budget"})
                        continue
                    # Do not consume the trigger merely because the AI call
                    # failed.  A failed/cooldown/data recheck is retryable; a
                    # successful AI decision is the commit point.
                    completed = self._recheck(
                        plan, trigger, session, account, account_ctx, quote, fields
                    )
                    if completed:
                        self.store.mark_trigger_fired(plan_id, trigger_id, _hash(fields))
                    break
                if purpose == "EXECUTE_ORDER":
                    # Fail closed when a simultaneous invalidation/recheck is
                    # also true: never submit while the plan is being questioned.
                    blocking = [
                        other for other in (candidate.get("triggers") or [])
                        if other.get("purpose") in {"INVALIDATE_PLAN", "REQUEST_AI_RECHECK"}
                        and not self.store.trigger_fired(plan_id, other.get("trigger_id"))
                        and self._conditions_true(other.get("all_of") or [], fields)
                    ]
                    if blocking:
                        continue
                if purpose != "EXECUTE_ORDER":
                    continue
                intent = as_intent_from_candidate(candidate)
                if intent is None:
                    continue
                attempt = self.store.trigger_attempts(plan_id, trigger_id)
                if attempt >= 3:
                    self.store.set_plan_status(plan_id, "NO_ORDER", "trigger attempts exhausted")
                    break
                if not self.store.mark_trigger_fired(plan_id, trigger_id, _hash(fields)):
                    continue
                current_session = session_info(self.clock(), self.execution)
                if not current_session.can_submit:
                    self.store.rearm_trigger(plan_id, trigger_id)
                    return "after_cutoff"
                try:
                    validate_new_intent(
                        intent, account=account, quote=quote, risk=self.risk,
                        execution=self.execution, symbols=self.symbols, session=current_session,
                        buy_attempts=buy_attempts, local_orders=self.store.unresolved_orders(),
                        freeze_reason=self.store.freeze_reason(),
                        stop_allowed=self.execution.allow_broker_stop and self.execution.broker_stop_verified,
                        paused=self.store.paused(),
                    )
                except BrokerStopUnsupported as exc:
                    self.store.set_plan_status(plan_id, "BROKER_STOP_UNSUPPORTED", str(exc))
                    self.store.event("intent_rejected", {"plan_id": plan_id, "trigger_id": trigger_id,
                                                          "reason": "BROKER_STOP_UNSUPPORTED"})
                    break
                except RiskRejected as exc:
                    self.store.event("intent_rejected", {"plan_id": plan_id, "trigger_id": trigger_id,
                                                          "reason": exc.code, "detail": exc.detail})
                    self.store.set_plan_status(plan_id, "NO_ORDER", f"risk:{exc.code}")
                    break
                result = self.orders.submit(
                    intent, plan_id=plan_id, trigger_id=trigger_id,
                    day=session.trading_day, session=current_session,
                    account=account, quote=quote, symbols=self.symbols, risk=self.risk,
                    buy_attempts=buy_attempts, freeze_reason=self.store.freeze_reason(),
                    attempt=attempt,
                )
                triggered += 1
                if result.get("state") == "NOT_SENT":
                    if attempt + 1 < 3 and current_session.can_submit and not self.store.freeze_reason():
                        self.store.rearm_trigger(plan_id, trigger_id)
                    else:
                        self.store.set_plan_status(plan_id, "NO_ORDER", "order not sent")
                elif result.get("unknown") or result.get("state") in {
                    "SUBMISSION_OUTCOME_UNKNOWN", "RECONCILIATION_REQUIRED",
                }:
                    self.store.set_plan_status(plan_id, "TRIGGERED", "submission outcome unknown")
                else:
                    self.store.set_plan_status(plan_id, "EXECUTED", "order submitted")
                break
        self.store.heartbeat("worker", ok=True, detail={"phase": "active", "plans": len(plans)})
        return "active_triggered" if triggered else "active_scan"

    @staticmethod
    def _conditions_true(conditions, fields) -> bool:
        if not conditions:
            return False
        for condition in conditions:
            field = str(condition.get("field") or "")
            value = fields.get(field)
            if value is None:
                return False
            try:
                target = float(condition.get("value"))
                actual = float(value)
            except (TypeError, ValueError):
                return False
            op = condition.get("op")
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

    # ----------------------------------------------------------------- recheck
    def _recheck_budget_exhausted(self, day) -> bool:
        try:
            calls = self.store.ai_calls_for_day(day, "INTRADAY_RECHECK")
        except Exception:  # noqa: BLE001
            return True
        return len(calls) >= self.execution.max_rechecks_per_day

    def _set_recheck_time(self, now):
        self.store.set_control("recheck_last_at", now.astimezone(timezone.utc).isoformat())

    def _recheck(self, plan, trigger, session, account, account_ctx, quote, fields):
        last = self.store.get_control("recheck_last_at")
        if last:
            try:
                if (self.clock() - _parse_iso(last)).total_seconds() < self.execution.recheck_cooldown_seconds:
                    return False
            except Exception:  # noqa: BLE001
                pass
        self._set_recheck_time(self.clock())
        payload = self._base_payload(session, account_ctx)
        payload["plan"] = plan.get("payload")
        payload["trigger"] = trigger
        payload["trigger_fields"] = fields
        payload["quote"] = quote
        candidate = (plan.get("payload") or {}).get("candidate") or {}
        symbol = candidate.get("symbol")
        if symbol and self.daily_data:
            try:
                daily = daily_history(self.daily_data(symbol), self.clock())
                intraday = self._intraday_for(symbol)
                payload["history"] = compact_history(daily, intraday)
            except Exception as exc:  # noqa: BLE001
                self.store.event("recheck_data", {
                    "symbol": symbol, "reason": type(exc).__name__ + ": " + str(exc)[:300],
                })
                # A recheck cannot authorize a trade on stale/missing data.
                return False
        try:
            result, meta = self.ai.complete(
                "INTRADAY_RECHECK", payload,
                lambda raw: parse_recheck(raw, plan_id=plan["plan_id"], now=self.clock()),
            )
        except Exception as exc:  # noqa: BLE001
            self.store.error("intraday_recheck", exc, outcome="recheck_failed")
            return False
        self.store.event("intraday_recheck", {
            "plan_id": plan["plan_id"], "trigger_id": trigger.get("trigger_id"),
            "decision": result.decision.value, "reason": result.reason, **meta,
        })
        if result.decision in {RecheckDecision.INVALIDATE, RecheckDecision.NO_TRADE}:
            self.store.set_plan_status(plan["plan_id"], "INVALIDATED" if result.decision is RecheckDecision.INVALIDATE else "NO_TRADE",
                                       result.reason)
            return True
        if result.decision is RecheckDecision.CONTINUE:
            return True
        if result.decision is RecheckDecision.REPLACE and result.order_intent:
            intent = result.order_intent.model_dump(mode="json")
            intent["side"] = intent.pop("action").lower()
            intent["stop_policy"] = str(intent.get("stop_policy") or "NONE")
            new_plan_id = f"{plan['plan_id']}-r{int(self.clock().timestamp())}"
            valid_until = result.plan_valid_until_utc or self.clock() + timedelta(minutes=5)
            synthetic = {
                "plan_id": new_plan_id,
                "mode": "INTRADAY_RECHECK",
                "trading_day": session.trading_day,
                "plan_valid_until_utc": valid_until.isoformat(),
                "candidate": {"symbol": intent["symbol"], "triggers": [], "order_intent": intent},
                "candidates": [{"plan_id": new_plan_id, "symbol": intent["symbol"], "triggers": []}],
            }
            self.store.save_plan(
                synthetic, config_hash=self.config.hash, trading_day=session.trading_day,
                supersede=False,
            )
            self.store.set_plan_status(plan["plan_id"], "SUPERSEDED", "recheck replace")
            current_session = session_info(self.clock(), self.execution)
            try:
                result_submit = self.orders.submit(
                    intent, plan_id=new_plan_id, trigger_id="recheck",
                    day=session.trading_day, session=current_session,
                    account=account, quote=quote, symbols=self.symbols, risk=self.risk,
                    buy_attempts=self.store.count_buy_attempts(session.trading_day),
                    freeze_reason=self.store.freeze_reason(), attempt=0,
                )
            except Exception as exc:  # noqa: BLE001
                self.store.error("intraday_recheck_submit", exc, outcome="recheck_order_failed")
                self.store.set_plan_status(new_plan_id, "NO_ORDER", "recheck order failed")
                return True
            if result_submit.get("state") == "NOT_SENT":
                self.store.set_plan_status(new_plan_id, "NO_ORDER", "recheck order not sent")
            elif result_submit.get("unknown") or result_submit.get("state") in {
                "SUBMISSION_OUTCOME_UNKNOWN", "RECONCILIATION_REQUIRED",
            }:
                self.store.set_plan_status(new_plan_id, "TRIGGERED", "recheck unknown")
            else:
                self.store.set_plan_status(new_plan_id, "EXECUTED", "recheck order submitted")
            return True
        return False

    # -------------------------------------------------------------- after close
    def after_close(self, session):
        if session.market_close is None:
            return "closed"
        now = self.clock()
        if now < session.market_close + timedelta(minutes=5):
            return "closed"
        key = f"{session.trading_day}:after_close"
        if not self.store.claim_stage(key, session.trading_day, "CLOSED", stale_seconds=600, max_attempts=1):
            return "closed"
        try:
            result = self.orders.reconcile()
            self.store.event("after_close_reconcile", {"trading_day": session.trading_day, **result})
            self.store.finish_stage(key, "DONE", result)
        except Exception as exc:  # noqa: BLE001
            self.store.error("after_close", exc, outcome="after_close_failed")
            self.store.finish_stage(key, "FAILED", {"error": type(exc).__name__})
        return "closed"

def as_intent_from_candidate(candidate: dict) -> dict | None:
    order = candidate.get("order_intent")
    if not isinstance(order, dict):
        return None
    intent = dict(order)
    intent["side"] = str(intent.pop("action", intent.get("action", ""))).lower()
    intent["stop_policy"] = str(intent.get("stop_policy") or "NONE")
    return intent
