"""Isolated historical broker simulator for the full exam replay.

Supports LIMIT entry fills, DAY/GTC validity, attached protective stops,
stop adjustment/removal, partial fills, gap/slippage, fees, borrow cost, long
and short positions, average entry and unrealized/realized P&L.

No Alpaca import and no network access.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from decimal import Decimal, ROUND_FLOOR
from uuid import uuid4

from exam_data.store import ET


def money(value) -> Decimal:
    return Decimal(str(value))


def _remaining(order) -> int:
    return max(0, int(order.qty) - int(order.filled_qty))


@dataclass
class ExamOrder:
    id: str
    symbol: str
    action: str
    qty: int
    order_type: str  # LIMIT or STOP
    time_in_force: str  # DAY or GTC
    submitted_at: datetime
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    status: str = "NEW"
    filled_qty: int = 0
    avg_fill_price: Decimal = Decimal("0")
    plan_id: str = ""
    parent_id: str = ""
    purpose: str = "ENTRY"  # ENTRY, EXIT, STOP
    reason: str = ""
    created_day: date | None = None
    events: list[dict] = field(default_factory=list)

    @property
    def remaining(self):
        return _remaining(self)


@dataclass
class ExamFill:
    order_id: str
    symbol: str
    action: str
    qty: int
    price: Decimal
    fee: Decimal
    at: datetime
    reason: str
    trigger_price: Decimal | None = None
    slippage: Decimal = Decimal("0")


@dataclass
class ClosedTrade:
    symbol: str
    direction: str
    qty: int
    entry_price: Decimal
    exit_price: Decimal
    entry_at: datetime
    exit_at: datetime
    pnl: Decimal
    fees: Decimal
    entry_order: str
    exit_order: str
    exit_reason: str


class ExamBroker:
    def __init__(
        self, symbols, *, initial_cash=Decimal("100000"),
        fee_per_share=Decimal("0.005"), slippage_bps=Decimal("5"),
        annual_borrow_rate=Decimal("0.08"), participation=Decimal("0.10"),
        short_locates=True,
    ):
        self.symbols = set(symbols)
        self.initial_cash = money(initial_cash)
        self.cash = self.initial_cash
        self.fee_per_share = fee_per_share
        self.slippage_bps = slippage_bps
        self.annual_borrow_rate = annual_borrow_rate
        self.participation = participation
        self.short_locates = bool(short_locates)
        self.positions: dict[str, int] = {}
        self.avg_entry: dict[str, Decimal] = {}
        self.entry_at: dict[str, datetime] = {}
        self.entry_order: dict[str, str] = {}
        self.short_proceeds: dict[str, Decimal] = {}
        self.orders: dict[str, ExamOrder] = {}
        self.fills: list[ExamFill] = []
        self.closed_trades: list[ClosedTrade] = []
        self.realized_pnl = Decimal("0")
        self.fees_paid = Decimal("0")
        self.borrow_cost = Decimal("0")
        self.events: list[dict] = []
        self.short_assumption = "ALL_SYMBOLS_ASSUMED_AVAILABLE_FOR_EXAM"

    # ------------------------------------------------------------- lifecycle
    def _event(self, kind, **data):
        self.events.append({"at": datetime.now().astimezone().isoformat(), "kind": kind, **data})

    def _new_id(self, prefix):
        return f"{prefix}-{uuid4().hex[:12]}"

    def submit(
        self, *, symbol, action, qty, limit_price, submitted_at, plan_id="",
        time_in_force="DAY", order_id=None, purpose="ENTRY", reason="",
        stop_policy="NONE", stop_loss=None,
    ):
        if symbol not in self.symbols:
            raise ValueError("outside fixed exam pool")
        if action not in {"OPEN_LONG", "CLOSE_LONG", "OPEN_SHORT", "COVER_SHORT", "BUY", "SELL", "REDUCE"}:
            raise ValueError("unsupported action")
        if isinstance(qty, bool) or not isinstance(qty, int) or qty <= 0:
            raise ValueError("whole positive shares required")
        if submitted_at.tzinfo is None:
            raise ValueError("timezone required")
        local = submitted_at.astimezone(ET)
        if not (time(9, 30) <= local.time() <= time(9, 59, 30)):
            raise ValueError("new local order outside 09:30-09:59:30")
        if time_in_force not in {"DAY", "GTC"}:
            raise ValueError("invalid time_in_force")
        price = money(limit_price)
        if price <= 0:
            raise ValueError("positive limit required")
        held = self.positions.get(symbol, 0)
        if action in {"CLOSE_LONG", "REDUCE", "SELL"}:
            if held <= 0 or qty > held:
                raise ValueError("cannot oversell or cross zero")
        if action == "COVER_SHORT":
            if held >= 0 or qty > abs(held):
                raise ValueError("cannot overcover or cross zero")
        if action in {"OPEN_LONG", "BUY"} and held < 0:
            raise ValueError("cover short before opening long")
        if action == "OPEN_SHORT":
            if held > 0:
                raise ValueError("close long before opening short")
            if not self.short_locates:
                raise ValueError("historical borrow locate unavailable")
        if stop_policy == "BROKER_STOP":
            if action not in {"OPEN_LONG", "OPEN_SHORT", "BUY"}:
                raise ValueError("BROKER_STOP only on opening")
            if stop_loss is None:
                raise ValueError("BROKER_STOP requires stop_loss")
            stop = money(stop_loss)
            if action in {"OPEN_LONG", "BUY"} and stop >= price:
                raise ValueError("long stop must be below limit")
            if action == "OPEN_SHORT" and stop <= price:
                raise ValueError("short stop must be above limit")
        elif stop_loss is not None:
            raise ValueError("NONE stop policy must not carry stop_loss")
        oid = order_id or self._new_id("exam")
        if oid in self.orders:
            raise ValueError("duplicate order id")
        order = ExamOrder(
            id=oid, symbol=symbol, action=action, qty=qty, order_type="LIMIT",
            time_in_force=time_in_force, submitted_at=submitted_at, limit_price=price,
            plan_id=plan_id, purpose=purpose, reason=reason, created_day=local.date(),
            stop_price=money(stop_loss) if stop_policy == "BROKER_STOP" else None,
        )
        # A stop price on the entry is attached only after fills.
        order.events.append({"at": submitted_at.isoformat(), "status": "NEW", "reason": reason})
        self.orders[oid] = order
        self._event("order_submitted", order_id=oid, symbol=symbol, action=action,
                    qty=qty, limit=str(price), tif=time_in_force,
                    stop=str(order.stop_price) if order.stop_price else None, plan_id=plan_id)
        return order

    def cancel(self, order_id, *, at, reason):
        order = self.orders.get(order_id)
        if order is None:
            raise ValueError("unknown order")
        if order.status in {"FILLED", "CANCELED", "EXPIRED", "REJECTED"}:
            return order
        order.status = "CANCELED"
        order.events.append({"at": at.isoformat(), "status": "CANCELED", "reason": reason})
        self._event("order_canceled", order_id=order_id, reason=reason)
        return order

    def active_stop(self, symbol):
        for order in self.orders.values():
            if order.symbol == symbol and order.purpose == "STOP" and order.status in {"NEW", "PARTIALLY_FILLED"}:
                return order
        return None

    def adjust_stop(self, symbol, *, stop_price, qty=None, at, reason, plan_id=""):
        stop = self.active_stop(symbol)
        if stop is None:
            raise ValueError("no active stop to adjust")
        held = abs(self.positions.get(symbol, 0))
        if held <= 0:
            raise ValueError("no position")
        value = money(stop_price)
        if value <= 0:
            raise ValueError("positive stop required")
        if qty is not None and (not isinstance(qty, int) or qty <= 0 or qty > held):
            raise ValueError("invalid stop qty")
        stop.stop_price = value
        stop.qty = qty or held
        stop.events.append({"at": at.isoformat(), "status": "ADJUSTED", "reason": reason,
                            "stop": str(value), "qty": stop.qty, "plan_id": plan_id})
        self._event("stop_adjusted", order_id=stop.id, symbol=symbol,
                    stop=str(value), qty=stop.qty, reason=reason)
        return stop

    def remove_stop(self, symbol, *, at, reason, plan_id=""):
        stop = self.active_stop(symbol)
        if stop is None:
            raise ValueError("no active stop to remove")
        self.cancel(stop.id, at=at, reason=reason)
        self._event("stop_removed", order_id=stop.id, symbol=symbol, reason=reason, plan_id=plan_id)
        return stop

    def set_stop(self, symbol, *, stop_price, at, reason, plan_id="", qty=None):
        """Create a GTC protective stop for an existing position."""
        held = self.positions.get(symbol, 0)
        if held == 0:
            raise ValueError("no position for stop")
        value = money(stop_price)
        if value <= 0:
            raise ValueError("positive stop required")
        current = self.active_stop(symbol)
        if current:
            return self.adjust_stop(symbol, stop_price=value, qty=qty, at=at,
                                    reason=reason, plan_id=plan_id)
        action = "SELL" if held > 0 else "BUY"
        stop = ExamOrder(
            id=self._new_id("stop"), symbol=symbol, action=action,
            qty=qty or abs(held), order_type="STOP", time_in_force="GTC",
            submitted_at=at, stop_price=value, plan_id=plan_id,
            purpose="STOP", reason=reason, created_day=at.astimezone(ET).date(),
        )
        stop.events.append({"at": at.isoformat(), "status": "NEW", "reason": reason})
        self.orders[stop.id] = stop
        self._event("stop_submitted", order_id=stop.id, symbol=symbol,
                    action=action, qty=stop.qty, stop=str(value),
                    plan_id=plan_id, reason=reason)
        return stop

    # --------------------------------------------------------------- execution
    def _create_attached_stop(self, entry, fill_at):
        if entry.stop_price is None or entry.purpose != "ENTRY":
            return None
        if self.active_stop(entry.symbol):
            stop = self.active_stop(entry.symbol)
            held = abs(self.positions.get(entry.symbol, 0))
            if held > 0:
                stop.qty = held
            return stop
        held = self.positions.get(entry.symbol, 0)
        if held == 0:
            return None
        action = "SELL" if held > 0 else "BUY"
        stop = ExamOrder(
            id=self._new_id("stop"), symbol=entry.symbol, action=action,
            qty=abs(held), order_type="STOP", time_in_force="GTC",
            submitted_at=fill_at, stop_price=entry.stop_price, plan_id=entry.plan_id,
            parent_id=entry.id, purpose="STOP", reason="attached BROKER_STOP",
            created_day=fill_at.astimezone(ET).date(),
        )
        stop.events.append({"at": fill_at.isoformat(), "status": "NEW", "reason": "attached"})
        self.orders[stop.id] = stop
        self._event("stop_submitted", order_id=stop.id, symbol=entry.symbol,
                    action=action, qty=stop.qty, stop=str(stop.stop_price),
                    parent=entry.id, plan_id=entry.plan_id)
        return stop

    def _apply_fill(self, order, qty, price, at, *, reason, trigger_price=None):
        if qty <= 0:
            return None
        action = order.action
        price = money(price)
        fee = self.fee_per_share * qty
        held = self.positions.get(order.symbol, 0)
        if action in {"CLOSE_LONG", "REDUCE", "SELL"}:
            qty = min(qty, max(0, held))
            if not qty:
                return None
        if action == "COVER_SHORT":
            qty = min(qty, max(0, -held))
            if not qty:
                return None
        buy = action in {"OPEN_LONG", "BUY", "COVER_SHORT"}
        if buy:
            free = self.cash - sum(self.short_proceeds.values(), Decimal("0"))
            if action in {"OPEN_LONG", "BUY"}:
                qty = min(qty, max(0, int((free / (price + self.fee_per_share)).to_integral_value(rounding=ROUND_FLOOR))))
            else:
                qty = min(qty, max(0, int((free / price).to_integral_value(rounding=ROUND_FLOOR))))
            fee = self.fee_per_share * qty
        if action == "OPEN_SHORT":
            free = self.cash - sum(self.short_proceeds.values(), Decimal("0"))
            qty = min(qty, max(0, int((free / (price * Decimal("0.5") + self.fee_per_share)).to_integral_value(rounding=ROUND_FLOOR))))
            fee = self.fee_per_share * qty
        if qty <= 0:
            return None
        signed = qty if buy else -qty
        prior = self.positions.get(order.symbol, 0)
        new = prior + signed
        # Realized P&L and average cost when closing.
        if prior != 0 and (prior > 0) != (signed > 0):
            close_qty = min(abs(prior), qty)
            entry = self.avg_entry.get(order.symbol, price)
            if prior > 0:
                pnl = (price - entry) * close_qty
            else:
                pnl = (entry - price) * close_qty
            self.realized_pnl += pnl
            self.closed_trades.append(ClosedTrade(
                order.symbol, "long" if prior > 0 else "short", close_qty,
                entry, price, self.entry_at.get(order.symbol, at), at,
                pnl, fee, self.entry_order.get(order.symbol, ""), order.id, reason,
            ))
            if prior + signed == 0:
                self.avg_entry.pop(order.symbol, None)
                self.entry_at.pop(order.symbol, None)
                self.entry_order.pop(order.symbol, None)
        else:
            # Opening or adding: weighted average.
            old_qty = max(0, prior)
            old_avg = self.avg_entry.get(order.symbol, Decimal("0"))
            total = old_qty + qty
            if old_qty:
                self.avg_entry[order.symbol] = (old_avg * old_qty + price * qty) / total
            else:
                self.avg_entry[order.symbol] = price
                self.entry_at[order.symbol] = at
                self.entry_order[order.symbol] = order.id
        if action == "OPEN_SHORT":
            self.short_proceeds[order.symbol] = self.short_proceeds.get(order.symbol, Decimal("0")) + price * qty
        elif action == "COVER_SHORT" and prior < 0:
            prior_proceeds = self.short_proceeds.get(order.symbol, Decimal("0"))
            closed = min(abs(prior), qty)
            self.short_proceeds[order.symbol] = prior_proceeds * (Decimal(abs(prior) - closed) / Decimal(abs(prior))) if abs(prior) else Decimal("0")
            if self.short_proceeds[order.symbol] == 0:
                self.short_proceeds.pop(order.symbol, None)
        self.cash -= signed * price + fee
        self.fees_paid += fee
        self.positions[order.symbol] = new
        if new == 0:
            self.positions.pop(order.symbol, None)
        order.filled_qty += qty
        order.avg_fill_price = ((order.avg_fill_price * (order.filled_qty - qty)) + price * qty) / order.filled_qty
        order.status = "FILLED" if order.filled_qty >= order.qty else "PARTIALLY_FILLED"
        order.events.append({"at": at.isoformat(), "status": order.status, "qty": qty,
                             "price": str(price), "reason": reason})
        fill = ExamFill(order.id, order.symbol, action, qty, price, fee, at, reason,
                        trigger_price, abs(price - money(trigger_price)) if trigger_price is not None else Decimal("0"))
        self.fills.append(fill)
        self._event("fill", order_id=order.id, symbol=order.symbol, action=action,
                    qty=qty, price=str(price), fee=str(fee), reason=reason,
                    trigger=str(trigger_price) if trigger_price is not None else None)
        if order.purpose == "ENTRY" and order.stop_price is not None:
            self._create_attached_stop(order, at)
        if order.purpose == "STOP":
            self._sync_stop_qty(order.symbol, at)
        return fill

    def _sync_stop_qty(self, symbol, at):
        stop = self.active_stop(symbol)
        if stop is None:
            return
        held = abs(self.positions.get(symbol, 0))
        if held <= 0:
            self.cancel(stop.id, at=at, reason="position closed")
        else:
            stop.qty = held

    def process_bar(self, bar, *, max_shares=None):
        if bar["timeframe"] != "1Min" or bar["feed"] != "iex":
            raise ValueError("only IEX 1Min replay bars")
        at = bar["end_at"]
        if at.tzinfo is None:
            raise ValueError("timezone required")
        local = at.astimezone(ET)
        if not (time(9, 30) < local.time() <= time(16, 0)):
            return []
        available = max(0, int((money(bar["volume"]) * self.participation).to_integral_value(rounding=ROUND_FLOOR)))
        if max_shares is not None:
            available = min(available, max_shares)
        filled = []
        # Protective stops first.  A gap through the stop is filled at the
        # adverse open, never at the stop price.
        stops = [o for o in self.orders.values() if o.purpose == "STOP" and o.symbol == bar["symbol"]
                 and o.status in {"NEW", "PARTIALLY_FILLED"}]
        for order in sorted(stops, key=lambda item: item.submitted_at):
            if available <= 0:
                break
            stop_price = order.stop_price
            if stop_price is None:
                continue
            if order.action == "SELL" and money(bar["low"]) <= stop_price:
                opening = money(bar["open"])
                raw = opening if opening <= stop_price else stop_price
                price = raw * (Decimal("1") - self.slippage_bps / Decimal("10000"))
                qty = min(order.remaining, available)
                fill = self._apply_fill(order, qty, price, at, reason="STOP_TRIGGERED",
                                        trigger_price=stop_price)
                if fill:
                    filled.append(fill); available -= fill.qty
            elif order.action == "BUY" and money(bar["high"]) >= stop_price:
                opening = money(bar["open"])
                raw = opening if opening >= stop_price else stop_price
                price = raw * (Decimal("1") + self.slippage_bps / Decimal("10000"))
                qty = min(order.remaining, available)
                fill = self._apply_fill(order, qty, price, at, reason="STOP_TRIGGERED",
                                        trigger_price=stop_price)
                if fill:
                    filled.append(fill); available -= fill.qty
        # Limits after stops.
        limits = [o for o in self.orders.values() if o.order_type == "LIMIT" and o.symbol == bar["symbol"]
                  and o.status in {"NEW", "PARTIALLY_FILLED"}]
        for order in sorted(limits, key=lambda item: item.submitted_at):
            if available <= 0:
                break
            limit = order.limit_price
            if limit is None:
                continue
            buy = order.action in {"OPEN_LONG", "BUY", "COVER_SHORT"}
            if buy and money(bar["low"]) > limit:
                continue
            if not buy and money(bar["high"]) < limit:
                continue
            opening = money(bar["open"])
            slip = self.slippage_bps / Decimal("10000")
            if buy:
                price = min(limit, opening * (Decimal("1") + slip)) if opening <= limit else limit
            else:
                price = max(limit, opening * (Decimal("1") - slip)) if opening >= limit else limit
            qty = min(order.remaining, available)
            fill = self._apply_fill(order, qty, price, at, reason="LIMIT_FILL")
            if fill:
                filled.append(fill); available -= fill.qty
        return filled

    def expire_day(self, day):
        for order in self.orders.values():
            if order.time_in_force == "DAY" and order.status in {"NEW", "PARTIALLY_FILLED"} \
                    and order.created_day == day:
                order.status = "EXPIRED"
                order.events.append({"at": datetime.now().astimezone().isoformat(),
                                     "status": "EXPIRED", "reason": "DAY order"})
                self._event("order_expired", order_id=order.id, symbol=order.symbol)
                if order.purpose == "STOP":
                    self._sync_stop_qty(order.symbol, order.submitted_at)

    def accrue_borrow(self, day, marks):
        cost = sum(
            abs(qty) * money(marks[s]) * self.annual_borrow_rate / Decimal("365")
            for s, qty in self.positions.items() if qty < 0
        )
        self.cash -= cost
        self.borrow_cost += cost
        return cost

    # ---------------------------------------------------------------- account
    def snapshot(self, asof, marks=None):
        return self.account(asof, marks or {})

    def account(self, asof, marks):
        positions = []
        gross = Decimal("0")
        long_mv = Decimal("0")
        short_mv = Decimal("0")
        for symbol, qty in sorted(self.positions.items()):
            if not qty:
                continue
            mark = money(marks.get(symbol, 0))
            avg = self.avg_entry.get(symbol, Decimal("0"))
            mv = mark * qty
            pnl = (mark - avg) * qty if qty > 0 else (avg - mark) * abs(qty)
            positions.append({
                "symbol": symbol, "qty": qty, "direction": "long" if qty > 0 else "short",
                "long_qty": max(0, qty), "short_qty": max(0, -qty), "whole_shares": abs(qty),
                "market_value": float(mv), "avg_entry_price": float(avg),
                "unrealized_pl": float(pnl),
                "unrealized_plpc": float(pnl / (avg * abs(qty))) if avg and qty else 0.0,
                "current_price": float(mark), "available_qty": abs(qty),
                "stops": [self._order_dict(o) for o in self.orders.values()
                          if o.symbol == symbol and o.purpose == "STOP" and o.status in {"NEW", "PARTIALLY_FILLED"}],
                "stop_status": "VERIFIED" if self.active_stop(symbol) else "NONE",
            })
            gross += abs(mv)
            if qty > 0: long_mv += mv
            else: short_mv += -mv
        equity = self.cash + sum(money(marks.get(s, 0)) * q for s, q in self.positions.items())
        reserved = sum(
            money(o.limit_price) * o.remaining for o in self.orders.values()
            if o.action in {"OPEN_LONG", "BUY", "COVER_SHORT"}
            and o.status in {"NEW", "PARTIALLY_FILLED"} and o.limit_price is not None
        )
        return {
            "source": "ISOLATED_SIMULATOR",
            "asof_utc": asof.astimezone(timezone.utc).isoformat(),
            "cash": float(self.cash), "equity": float(equity),
            "buying_power": float(max(Decimal("0"), self.cash - reserved)),
            "available_non_margin_cash": float(max(Decimal("0"), self.cash - reserved)),
            "reserved_buy_cash": float(reserved),
            "long_market_value": float(long_mv), "short_market_value": float(short_mv),
            "gross_exposure": float(gross),
            "shorting_enabled": True, "allow_shorting": True,
            "positions": {s: q for s, q in self.positions.items()},
            "position_details": {p["symbol"]: p for p in positions},
            "positions_detail": positions,
            "open_orders": [self._order_dict(o) for o in self.orders.values()
                            if o.status in {"NEW", "PARTIALLY_FILLED"}],
            "stops": [self._order_dict(o) for o in self.orders.values()
                      if o.purpose == "STOP" and o.status in {"NEW", "PARTIALLY_FILLED"}],
            "historical_short_locates": {s: "ASSUMED_AVAILABLE" for s in sorted(self.symbols)},
            "account_snapshot_id": self._account_id(asof, equity, positions),
            "realized_pnl": float(self.realized_pnl),
            "unrealized_pnl": float(sum(Decimal(str(p["unrealized_pl"])) for p in positions)),
            "fees_paid": float(self.fees_paid),
            "borrow_cost": float(self.borrow_cost),
            "short_locate_assumption": self.short_assumption,
        }

    def _account_id(self, asof, equity, positions):
        payload = {
            "asof": asof.isoformat(), "cash": str(self.cash), "equity": str(equity),
            "positions": {p["symbol"]: p["qty"] for p in positions},
            "orders": [(o.id, o.status, o.filled_qty) for o in self.orders.values()],
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    @staticmethod
    def _order_dict(order):
        return {
            "id": order.id, "client_order_id": order.id, "symbol": order.symbol,
            "side": "buy" if order.action in {"OPEN_LONG", "BUY", "COVER_SHORT"} else "sell",
            "position_intent": order.action, "qty": order.qty, "filled_qty": order.filled_qty,
            "type": "stop" if order.order_type == "STOP" else "limit",
            "time_in_force": order.time_in_force,
            "limit_price": str(order.limit_price) if order.limit_price is not None else None,
            "stop_price": str(order.stop_price) if order.stop_price is not None else None,
            "status": order.status.lower(), "order_class": "simple",
            "parent_id": order.parent_id, "plan_id": order.plan_id,
            "purpose": order.purpose, "created_at": order.submitted_at.isoformat(),
        }

    def open_positions(self, marks):
        out = []
        for symbol, qty in self.positions.items():
            if not qty:
                continue
            mark = money(marks.get(symbol, 0))
            avg = self.avg_entry.get(symbol, Decimal("0"))
            pnl = (mark - avg) * qty if qty > 0 else (avg - mark) * abs(qty)
            out.append({
                "symbol": symbol, "qty": qty, "direction": "long" if qty > 0 else "short",
                "entry_price": float(avg), "mark": float(mark),
                "market_value": float(mark * qty), "unrealized_pnl": float(pnl),
                "entry_at": self.entry_at.get(symbol).isoformat() if self.entry_at.get(symbol) else None,
                "stop": self._order_dict(self.active_stop(symbol)) if self.active_stop(symbol) else None,
            })
        return out

    def serialize(self):
        return {
            "cash": str(self.cash), "positions": dict(self.positions),
            "avg_entry": {k: str(v) for k, v in self.avg_entry.items()},
            "realized_pnl": str(self.realized_pnl), "fees_paid": str(self.fees_paid),
            "borrow_cost": str(self.borrow_cost),
            "orders": [self._order_dict(o) | {"events": o.events} for o in self.orders.values()],
            "fills": [f.__dict__ | {"price": str(f.price), "fee": str(f.fee),
                                    "trigger_price": str(f.trigger_price) if f.trigger_price is not None else None,
                                    "slippage": str(f.slippage)} for f in self.fills],
            "closed_trades": [t.__dict__ | {"entry_price": str(t.entry_price),
                                            "exit_price": str(t.exit_price),
                                            "pnl": str(t.pnl), "fees": str(t.fees)}
                              for t in self.closed_trades],
        }
