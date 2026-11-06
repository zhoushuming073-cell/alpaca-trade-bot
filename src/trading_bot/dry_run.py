"""Offline, synthetic dry run for the new session engine.

This is a plumbing fixture, not an investment signal and not a Paper order.
It exercises premarket plan -> trigger -> DAY limit submission -> reconcile.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pandas as pd

from .decision_schema import (
    Candidate,
    Condition,
    MarketDecision,
    OpportunityGrade,
    OrderIntent,
    PremarketPlan,
    ScanCandidate,
    SetupType,
    StopPolicy,
    TradeDecision,
    Trigger,
    TriggerPurpose,
    UniverseScan,
)
from .engine import Engine
from .market_data import CALENDAR
from .settings import load_config


class FakeBroker:
    def __init__(self, *, fill=True, prepare_error=None, dispatch_timeout=False,
                 shorting_enabled=False, shortable=True):
        self.orders = {}
        self.submissions = 0
        self.lookup_calls = 0
        self.cancel_calls = []
        self.fill = fill
        self.prepare_error = prepare_error
        self.dispatch_timeout = dispatch_timeout
        self.cash = 100000.0
        self.positions = {}
        self.open = True
        self.shorting_enabled = shorting_enabled
        self.shortable = shortable

    def snapshot(self):
        open_orders = [
            dict(order) for order in self.orders.values()
            if str(order.get("status")) not in {"filled", "canceled", "expired", "rejected"}
        ]
        position_details = {}
        for symbol, qty in self.positions.items():
            market_value = qty * 100.0
            position_details[symbol] = {
                "symbol": symbol, "qty": qty,
                "direction": "long" if qty > 0 else "short" if qty < 0 else "flat",
                "long_qty": max(0.0, qty), "short_qty": max(0.0, -qty),
                "market_value": market_value,
                "avg_entry_price": 100.0, "unrealized_pl": 0.0,
                "unrealized_plpc": 0.0, "current_price": 100.0,
                "qty_available": abs(qty),
            }
        return {
            "account_id_hash": "offline-fixture",
            "equity": self.cash + sum(q * 100.0 for q in self.positions.values()),
            "last_equity": 100000.0,
            "cash": self.cash,
            "buying_power": max(0.0, self.cash),
            "non_margin_cash": max(0.0, self.cash),
            "shorting_enabled": self.shorting_enabled,
            "short_market_value": sum(q * 100.0 for q in self.positions.values() if q < 0),
            "blocked": False,
            "positions": {s: q * 100.0 for s, q in self.positions.items()},
            "position_details": position_details,
            "quantities": dict(self.positions),
            "available_quantities": {s: abs(q) for s, q in self.positions.items()},
            "open_orders": open_orders,
            "asof": datetime.now(timezone.utc).isoformat(),
        }

    def asset_status(self, symbol):
        return {
            "symbol": symbol, "tradable": True, "shortable": self.shortable,
            "easy_to_borrow": self.shortable, "marginable": self.shortable,
            "status": "active", "asset_class": "us_equity",
        }

    def is_open(self):
        return self.open

    def prepare(self, intent, cid, session=None, account=None):
        if self.prepare_error:
            from .safety import PreDispatchRejected

            raise PreDispatchRejected(str(self.prepare_error))
        if not self.open:
            from .safety import PreDispatchRejected

            raise PreDispatchRejected("offline broker closed")
        return self.snapshot()

    def dispatch(self, intent, cid):
        self.submissions += 1
        qty = int(intent["qty"])
        fill_qty = qty if self.fill else 0
        status = "filled" if self.fill else "accepted"
        order = {
            "id": "fake-" + cid,
            "client_order_id": cid,
            "symbol": intent["symbol"],
            "side": intent["side"],
            "position_intent": intent.get("position_intent"),
            "qty": str(qty),
            "filled_qty": str(fill_qty),
            "filled_avg_price": str(intent["limit_price"]) if fill_qty else None,
            "type": "limit",
            "time_in_force": "day",
            "limit_price": str(intent["limit_price"]),
            "status": status,
            "synthetic": True,
        }
        self.orders[cid] = order
        if fill_qty:
            signed = fill_qty if intent["side"] == "buy" else -fill_qty
            self.positions[intent["symbol"]] = self.positions.get(intent["symbol"], 0) + signed
            self.cash -= signed * float(intent["limit_price"])
        if self.dispatch_timeout:
            raise TimeoutError("offline POST response lost")
        return dict(order)

    def lookup(self, cid):
        self.lookup_calls += 1
        return self.orders.get(cid)

    def cancel(self, cid):
        self.cancel_calls.append(cid)
        if cid in self.orders:
            self.orders[cid]["status"] = "canceled"

    def quote(self, symbol, risk=None, close=None):
        return {
            "bid": 99.99, "ask": 100.01, "last": 100.0,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "last_timestamp": datetime.now(timezone.utc).isoformat(),
            "quote_age_seconds": 0.0, "last_age_seconds": 0.0, "source": "synthetic",
        }

    def advance_fill(self, cid):
        order = self.orders[cid]
        if order["status"] == "accepted":
            qty = int(order["qty"])
            order["status"] = "filled"
            order["filled_qty"] = str(qty)
            signed = qty if order["side"] == "buy" else -qty
            self.positions[order["symbol"]] = self.positions.get(order["symbol"], 0) + signed
            self.cash -= signed * float(order["limit_price"])


class ScriptedAI:
    model = "offline-fixture"

    def __init__(self, *, symbol="CRWV", qty=3, limit_price=100.0, stop_policy="NONE"):
        self.symbol = symbol
        self.qty = qty
        self.limit_price = limit_price
        self.stop_policy = stop_policy
        self.calls = []

    def complete(self, mode, payload, parser):
        self.calls.append(mode)
        now = datetime.fromisoformat(str(payload["as_of_utc"]).replace("Z", "+00:00"))
        meta = {
            "mode": mode, "model": self.model, "resolved_model": self.model,
            "latency_ms": 0, "attempts": 1, "prompt_tokens": 0,
            "completion_tokens": 0, "finish_reason": "stop",
            "prompt_hash": "offline", "input_hash": "offline",
            "output_hash": "offline", "repair_used": False,
        }
        if mode == "UNIVERSE_SCAN":
            return UniverseScan(
                as_of_utc=now, model_id=self.model,
                account_snapshot_id=payload["account_snapshot_id"],
                candidates=[ScanCandidate(
                    symbol=self.symbol, setup_type=SetupType.PULLBACK_RECLAIM,
                    opportunity_grade=OpportunityGrade.A, trade_decision=TradeDecision.BUY,
                    thesis="offline fixture; no investment signal", priority=1,
                )],
            ), meta
        if mode == "PREMARKET_PLAN":
            candidate = Candidate(
                symbol=self.symbol, setup_type=SetupType.PULLBACK_RECLAIM,
                opportunity_grade=OpportunityGrade.A, trade_decision=TradeDecision.BUY,
                analysis="Offline plumbing fixture; not an investment signal.",
                plan_id="offline-plan-1",
                plan_valid_until_utc=now + timedelta(minutes=50),
                triggers=[Trigger(
                    trigger_id="offline-exec", purpose=TriggerPurpose.EXECUTE_ORDER,
                    all_of=[Condition(field="last", op="<=", value=101.0)],
                )],
                order_intent=OrderIntent(
                    action="BUY", symbol=self.symbol, qty=self.qty,
                    limit_price=self.limit_price, stop_policy=StopPolicy(self.stop_policy),
                    stop_loss=None,
                ),
            )
            return PremarketPlan(
                as_of_utc=now, model_id=self.model,
                market_decision=MarketDecision.TRADE,
                account_snapshot_id=payload["account_snapshot_id"],
                candidates=[candidate],
            ), meta
        raise AssertionError("unexpected offline AI mode: " + mode)


def _days(now):
    today = pd.Timestamp(now).tz_convert("America/New_York").date()
    schedule = CALENDAR.schedule(
        pd.Timestamp(today) - pd.DateOffset(months=6),
        pd.Timestamp(today) - pd.Timedelta(days=1),
    )
    index = pd.DatetimeIndex(schedule.index).tz_localize("America/New_York")
    return pd.DataFrame(
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1000},
        index=index,
    )


def _minutes(now):
    stamp = pd.Timestamp(now).floor("5min")
    index = pd.date_range(end=stamp, periods=80, freq="5min")
    return pd.DataFrame(
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 10},
        index=index,
    )


def run(store, bundle=None, *, now=None, fill=True):
    bundle = bundle or load_config()
    # The deterministic dry-run fixture exercises the single-agent path;
    # production multi-agent behavior is tested separately.
    if bundle.multi_agent.enabled:
        bundle = replace(bundle, multi_agent=bundle.multi_agent.model_copy(update={"enabled": False}))
    now = now or datetime(2026, 11, 5, 14, 5, 10, tzinfo=timezone.utc)
    clock = [now]
    broker = FakeBroker(fill=fill)
    ai = ScriptedAI(symbol=bundle.symbols[0])
    engine = Engine(
        store, broker, ai, bundle,
        data=lambda symbol: _minutes(clock[0]),
        daily_data=lambda symbol: _days(clock[0]),
        clock=lambda: clock[0],
        mode="dry-run",
        require_watchdog=False,
        check_disk_config=False,
    )
    premarket = engine.tick()
    clock[0] = datetime(2026, 11, 5, 14, 35, 10, tzinfo=timezone.utc)
    active = engine.tick()
    return {
        "premarket": premarket,
        "active": active,
        "submissions": broker.submissions,
        "positions": dict(broker.positions),
        "safety": store.safety_reason(),
        "ai_calls": ai.calls,
    }
