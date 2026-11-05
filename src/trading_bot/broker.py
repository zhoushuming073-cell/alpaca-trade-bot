"""Alpaca Paper-only broker adapter and the execution gateway.

The gateway has two deliberately separate phases:

``prepare``
    Slow, read-only checks (clock, asset, account, sellable shares, stop
    capability).  Failures here are deterministic and can be resolved as
    ``NOT_SENT`` without a broker lookup.

``dispatch``
    Fast wire check against the durable intent and the already-recorded
    dispatch boundary, followed by exactly one SDK POST.  No slow checks are
    allowed after the dispatch boundary, because an exception there would be
    indistinguishable from an accepted POST.

The trading client is the same injected Alpaca SDK client used by the broker,
so success responses are returned synchronously and ``client_order_id`` is
stable.  The HTTP adapter bounds connect/read timeouts and disables transport
retries for writes.
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
import time
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone

from requests.adapters import HTTPAdapter

from .network import read_retry
from .safety import (
    BrokerStopUnsupported,
    OrderValidationError,
    PreDispatchRejected,
    validate_intent,
    validate_order,
)
from .settings import PAPER_URL, load_config


def build_broker(keys, start_orders_thread=None):
    from alpaca.trading.client import TradingClient
    from lumibot.brokers import Alpaca
    from lumibot.data_sources import AlpacaData

    class BoundedAdapter(HTTPAdapter):
        def send(self, request, **kwargs):
            kwargs["timeout"] = (3, 5) if request.method == "DELETE" or "by_client_order_id" in request.url else (5, 20)
            return super().send(request, **kwargs)

    class PaperClient(TradingClient):
        @contextmanager
        def fresh_reads(self):
            previous = getattr(self.permits, "fresh", False)
            self.permits.fresh = True
            try:
                yield
            finally:
                self.permits.fresh = previous

        def _request(self, method, path, data=None, **kwargs):
            if str(self._base_url).rstrip("/") != PAPER_URL or kwargs.get("base_url", PAPER_URL) != PAPER_URL:
                raise RuntimeError("Paper endpoint invariant failed")
            from .market_data import execution_open

            verb = str(method).upper()
            cacheable = verb == "GET" and path in {"/account", "/positions", "/orders", "/clock"}
            cache_key = (path, json.dumps(data, sort_keys=True, default=str))
            if cacheable and not getattr(self.permits, "fresh", False):
                idle = getattr(self, "idle", lambda: False)()
                ttl = 30 if not idle and execution_open(datetime.now(timezone.utc)) else 300
                with self.cache_lock:
                    cached = self.read_cache.get(cache_key)
                if cached and time.monotonic() - cached[0] < ttl:
                    return deepcopy(cached[1])
            if verb in {"POST", "PATCH", "PUT", "DELETE"}:
                if verb == "POST" and path == "/orders":
                    # The permit is the single-use, durable authorization from
                    # PaperGateway.dispatch.  It is consumed even on failure.
                    quantity = validate_order(data or {}, allow_stop=True)
                    data = dict(data)
                    data["qty"] = str(quantity)
                    permit = getattr(self.permits, "current", None)
                    self.permits.current = None
                    if not permit:
                        raise RuntimeError("Direct order bypass forbidden: durable execution authorization required")
                    permit(data)
                elif verb == "DELETE" and path.startswith("/orders/"):
                    pass  # explicit cancellation only; the watchdog never calls this
                else:
                    raise RuntimeError("Replacement, bulk submission and position liquidation endpoints disabled")
            operation = lambda: super(PaperClient, self)._request(method, path, data, **kwargs)
            result = read_retry(operation, getattr(self, "audit", None)) if verb == "GET" else operation()
            with self.cache_lock:
                if cacheable:
                    self.read_cache[cache_key] = (time.monotonic(), deepcopy(result))
                elif verb != "GET":
                    self.read_cache.clear()
            return result

    client = PaperClient(keys["ALPACA_API_KEY"], keys["ALPACA_API_SECRET"], paper=True, url_override=PAPER_URL)
    client._retry = 0
    client.permits = threading.local()
    client.read_cache = {}
    client.cache_lock = threading.RLock()
    client._session.mount("https://", BoundedAdapter(max_retries=0))
    cfg = {"API_KEY": keys["ALPACA_API_KEY"], "API_SECRET": keys["ALPACA_API_SECRET"], "PAPER": True, "DATA_FEED": "iex"}
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.enums import DataFeed

    class IEXClient(StockHistoricalDataClient):
        def _request(self, method, path, data=None, **kwargs):
            operation = lambda: super(IEXClient, self)._request(method, path, data, **kwargs)
            return read_retry(operation, getattr(client, "audit", None)) if str(method).upper() == "GET" else operation()

        def get_stock_bars(self, request):
            request.feed = DataFeed.IEX
            if request.timeframe.unit.value == "Day":
                from alpaca.data.enums import Adjustment

                request.adjustment = Adjustment.SPLIT
            return super().get_stock_bars(request)

    stock = IEXClient(keys["ALPACA_API_KEY"], keys["ALPACA_API_SECRET"])
    stock._retry = 0
    stock._session.mount("https://", BoundedAdapter(max_retries=0))

    class UncachedIEX:
        # The wrapper has no credential attributes, so LumiBot's shared bar
        # cache bypasses reuse and every decision gets a fresh provider read.
        def get_stock_bars(self, request):
            return stock.get_stock_bars(request)

        def get_stock_latest_quote(self, request):
            return stock.get_stock_latest_quote(request)

        def get_stock_latest_trade(self, request):
            return stock.get_stock_latest_trade(request)

    class RegularIEXData(AlpacaData):
        def supports_native_timestep(self, timestep):
            return timestep in ("5 minutes", "5min", "5minute")

        def _get_dataframe_from_api(self, asset, length, **kwargs):
            from .market_data import regular_bars

            df = super()._get_dataframe_from_api(
                asset,
                length=length if kwargs.get("timestep") == "day" else max(length * 6, 800),
                **kwargs,
            )
            return (df.tail(length) if kwargs.get("timestep") == "day" else regular_bars(df).tail(length)) if df is not None else None

    data = RegularIEXData(cfg, delay=0, stock_client=UncachedIEX(), auto_adjust=False)

    class GuardedAlpaca(Alpaca):
        def _submit_order(self, order):
            payload = {
                "symbol": order.asset.symbol,
                "qty": order.quantity,
                "side": order.side,
                "limit_price": order.limit_price,
                "type": order.order_type,
                "time_in_force": order.time_in_force,
                "order_class": order.order_class,
            }
            payload.update(order.custom_params or {})
            validate_order(payload, allow_stop=True)
            if order.asset.asset_type != "stock":
                raise ValueError("Only US equity supported")
            return super()._submit_order(order)

        def _submit_orders(self, orders, **kwargs):
            raise ValueError("Bulk/multileg execution disabled; use audited single-order gateway")

    return GuardedAlpaca(cfg, trading_client=client, data_source=data, connect_stream=False, start_orders_thread=start_orders_thread)


class PaperGateway:
    """Paper-only, long-only stock gateway with explicit prepare/dispatch."""

    def __init__(self, strategy, store=None, symbols=None, execution=None):
        self.strategy = strategy
        self.api = strategy.broker.api
        self.store = store
        bundle = None if symbols is not None and execution is not None else load_config()
        self.symbols = set(symbols if symbols is not None else bundle.symbols)
        self.execution = execution if execution is not None else bundle.execution
        if store:
            self.api.audit = store.event
            self.api.idle = lambda: store.paused() or bool(store.safety_reason())
        self._last_account_number = ""
        self._quote_lock = threading.Lock()

    # ---------------------------------------------------------------- snapshot
    def snapshot(self, include_history: bool = False):
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus

        recent_orders = []
        recent_orders_error = None
        with self.api.fresh_reads():
            account = self.api.get_account()
            positions = self.api.get_all_positions()
            orders = self.api.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500))
            if include_history:
                try:
                    recent_orders = self.api.get_orders(
                        filter=GetOrdersRequest(status=QueryOrderStatus.CLOSED, limit=50)
                    )
                except Exception as exc:  # noqa: BLE001 - history is optional
                    # The live account/open-order path must not fail just
                    # because the optional recent-history query jittered.
                    recent_orders_error = f"{type(exc).__name__}: {str(exc)[:200]}"
                    recent_orders = []
        if len(orders) >= 500:
            raise ValueError("Open order result may be truncated; execution blocked")
        account_number = str(getattr(account, "account_number", "") or "")
        if account_number:
            self._last_account_number = hashlib.sha256(account_number.encode()).hexdigest()[:16]
        positions_by_symbol = {}
        quantities = {}
        available = {}
        for position in positions:
            symbol = position.symbol
            qty = float(position.qty)
            qty_available = float(position.qty_available if position.qty_available is not None else qty)
            quantities[symbol] = qty
            available[symbol] = qty_available
            positions_by_symbol[symbol] = {
                "symbol": symbol,
                "qty": qty,
                "qty_available": qty_available,
                "market_value": float(position.market_value),
                "avg_entry_price": float(position.avg_entry_price or 0),
                "unrealized_pl": float(position.unrealized_pl or 0),
                "unrealized_plpc": float(getattr(position, "unrealized_plpc", 0) or 0),
                "current_price": float(position.current_price or 0),
                "change_today": float(getattr(position, "change_today", 0) or 0),
                "side": str(getattr(getattr(position, "side", None), "value", getattr(position, "side", "")) or "").lower(),
            }
        open_orders = [order.model_dump(mode="json") for order in orders]
        from .risk_manager import reserved_buy_cash

        account_view = {
            "open_orders": open_orders,
            "cash": float(account.cash),
        }
        reserved_buy = reserved_buy_cash(account_view)
        equity = float(account.equity)
        asof = datetime.now(timezone.utc).isoformat()
        return {
            "account_id_hash": self._last_account_number,
            "equity": equity,
            "last_equity": float(account.last_equity),
            "portfolio_value": float(getattr(account, "portfolio_value", equity) or equity),
            "cash": float(account.cash),
            "buying_power": float(account.buying_power),
            # Never treat margin buying power as spendable cash.
            "non_margin_cash": max(0.0, float(account.cash)),
            "reserved_buy_cash": reserved_buy,
            "available_non_margin_cash": max(0.0, float(account.cash) - reserved_buy),
            "long_market_value": float(getattr(account, "long_market_value", 0) or 0),
            "short_market_value": float(getattr(account, "short_market_value", 0) or 0),
            "initial_margin": float(getattr(account, "initial_margin", 0) or 0),
            "maintenance_margin": float(getattr(account, "maintenance_margin", 0) or 0),
            "daytrade_count": int(getattr(account, "daytrade_count", 0) or 0),
            "currency": str(getattr(account, "currency", "USD") or "USD"),
            "account_status": str(getattr(getattr(account, "status", None), "value", getattr(account, "status", "")) or ""),
            "blocked": bool(account.trading_blocked or account.account_blocked),
            "trading_blocked": bool(account.trading_blocked),
            "account_blocked": bool(account.account_blocked),
            "positions": {symbol: item["market_value"] for symbol, item in positions_by_symbol.items()},
            "position_details": positions_by_symbol,
            "quantities": quantities,
            "available_quantities": available,
            "open_orders": open_orders,
            "recent_orders": [
                order.model_dump(mode="json") if hasattr(order, "model_dump") else dict(order)
                for order in (recent_orders or [])
            ],
            "recent_orders_error": recent_orders_error,
            "asof": asof,
            "ts": asof,
        }

    def is_open(self):
        with self.api.fresh_reads():
            clock = self.api.get_clock()
        if abs((datetime.now(timezone.utc) - clock.timestamp).total_seconds()) > 5:
            raise ValueError("Local/broker clock skew exceeds five seconds")
        return clock.is_open

    def validate_assets(self, symbols):
        for symbol in symbols:
            asset = self.api.get_asset(symbol)
            if not asset.tradable or str(asset.status.value) != "active" or asset.asset_class.value != "us_equity":
                raise ValueError("Watchlist contains unavailable US equity: " + symbol)

    def lookup(self, cid):
        from alpaca.common.exceptions import APIError

        try:
            result = self.api.get_order_by_client_id(cid)
            return result.model_dump(mode="json") if hasattr(result, "model_dump") else dict(result)
        except APIError as exc:
            if exc.status_code == 404:
                return None
            raise

    # ----------------------------------------------------------------- prepare
    def _config_drift(self) -> bool:
        """Detect disk/active drift even if the engine has not ticked yet."""
        if not self.store:
            return False
        try:
            disk = load_config()
        except Exception:
            return True
        active = self.store.active_config() or {}
        active_hash = (active.get("hashes") or {}).get("combined")
        return bool(active_hash and active_hash != disk.hash)

    def prepare(self, intent, cid, *, session=None, account=None):
        """Slow pre-dispatch checks.  Raises :class:`PreDispatchRejected`."""
        from .market_data import session_info

        symbol = intent.get("symbol")
        stop_policy = str(intent.get("stop_policy") or "NONE").upper()
        if stop_policy == "BROKER_STOP" and not (
            self.execution.allow_broker_stop and self.execution.broker_stop_verified
        ):
            raise BrokerStopUnsupported(
                "BROKER_STOP requested but not verified for this Alpaca Paper account"
            )
        try:
            validate_intent(intent, allow_stop=self.execution.allow_broker_stop and self.execution.broker_stop_verified)
        except OrderValidationError as exc:
            raise PreDispatchRejected("INVALID_ORDER: " + str(exc)) from exc
        if symbol not in self.symbols:
            raise PreDispatchRejected("Outside fixed watchlist")
        if not self.store or not self.store.order_row(cid):
            raise PreDispatchRejected("Missing durable order request")
        if self.store.paused() or self.store.safety_reason():
            raise PreDispatchRejected("Execution paused or safety-locked")
        if self.store.config_pending() or self._config_drift():
            raise PreDispatchRejected("Configuration pending restart")
        if session is None:
            session = session_info(datetime.now(timezone.utc), self.execution)
        if not session.can_submit:
            raise PreDispatchRejected("Outside 09:30-10:00 ET submission window")
        if not self.is_open():
            raise PreDispatchRejected("Market closed before submission")
        asset = self.api.get_asset(symbol)
        if not asset.tradable or asset.status.value != "active" or asset.asset_class.value != "us_equity":
            raise PreDispatchRejected("Unavailable US equity")
        if str(intent.get("stop_policy") or "NONE").upper() == "BROKER_STOP":
            if not (self.execution.allow_broker_stop and self.execution.broker_stop_verified):
                raise BrokerStopUnsupported("BROKER_STOP is not verified for this Paper account")
            if str(intent.get("side")).lower() != "buy":
                raise BrokerStopUnsupported("BROKER_STOP is only supported for BUY intents in this build")
        fresh = self.snapshot()
        if str(intent.get("side")).lower() == "sell":
            from .risk_manager import sellable_qty

            available = sellable_qty(fresh, symbol)
            if int(intent["qty"]) > available:
                raise PreDispatchRejected(
                    f"Fresh available whole shares insufficient: need {intent['qty']}, have {available}"
                )
        return fresh

    # ---------------------------------------------------------------- dispatch
    def dispatch(self, intent, cid):
        """Recorded-boundary POST.  No slow checks are allowed here."""
        from decimal import Decimal
        from alpaca.trading.requests import LimitOrderRequest, StopLossRequest
        from alpaca.trading.enums import OrderClass

        validate_intent(intent, allow_stop=self.execution.allow_broker_stop and self.execution.broker_stop_verified)
        row = self.store.order_row(cid) if self.store else None
        if not row:
            raise PreDispatchRejected("Missing durable order request")
        from .order_state import OrderState

        if row["state"] != OrderState.DISPATCH_INTENT_RECORDED.value or not self.store.dispatched(cid):
            raise RuntimeError("Dispatch boundary was not durably recorded")
        stored_intent = json.loads(row["intent"] or "{}")
        for key in ("symbol", "side", "qty", "limit_price", "stop_policy"):
            if str(stored_intent.get(key)) != str(intent.get(key)):
                raise RuntimeError("Wire order differs from durable risk-approved intent")
        stop_policy = str(intent.get("stop_policy") or "NONE").upper()
        order_class = OrderClass.OTO if stop_policy == "BROKER_STOP" else OrderClass.SIMPLE
        stop_loss = None
        if stop_policy == "BROKER_STOP":
            stop = intent.get("stop_loss") or {}
            stop_price = stop.get("stop_price") if isinstance(stop, dict) else stop
            stop_loss = StopLossRequest(stop_price=float(stop_price))
        request = LimitOrderRequest(
            symbol=intent["symbol"],
            qty=int(intent["qty"]),
            side=str(intent["side"]),
            limit_price=float(intent["limit_price"]),
            time_in_force="day",
            order_class=order_class,
            stop_loss=stop_loss,
            client_order_id=cid,
            extended_hours=False,
        )

        def guard(wire):
            current = self.store.order_row(cid)
            if not current or current["state"] != OrderState.DISPATCH_INTENT_RECORDED.value:
                raise RuntimeError("Order authorization state changed before POST")
            if not self.store.dispatched(cid):
                raise RuntimeError("Dispatch boundary missing before POST")
            expected = json.loads(current["intent"] or "{}")
            if wire.get("client_order_id") != cid:
                raise RuntimeError("Wire client_order_id mismatch")
            if str(wire.get("symbol")) != str(expected["symbol"]) or str(
                getattr(wire.get("side"), "value", wire.get("side"))
            ).lower() != str(expected["side"]).lower():
                raise RuntimeError("Wire symbol/side mismatch")
            if Decimal(str(wire["qty"])) != Decimal(str(expected["qty"])):
                raise RuntimeError("Wire qty mismatch")
            if Decimal(str(wire["limit_price"])) != Decimal(str(expected["limit_price"])):
                raise RuntimeError("Wire limit price mismatch")
            if str(getattr(wire.get("time_in_force"), "value", wire.get("time_in_force"))).lower() != "day":
                raise RuntimeError("Wire time_in_force mismatch")
            if str(getattr(wire.get("type"), "value", wire.get("type"))).lower() != "limit":
                raise RuntimeError("Wire type mismatch")
            if wire.get("extended_hours"):
                raise RuntimeError("Extended hours forbidden")
            if stop_policy == "BROKER_STOP":
                wire_stop = wire.get("stop_loss") or {}
                wire_price = wire_stop.get("stop_price") if isinstance(wire_stop, dict) else getattr(wire_stop, "stop_price", None)
                expected_stop = (expected.get("stop_loss") or {}).get("stop_price") if isinstance(expected.get("stop_loss"), dict) else expected.get("stop_loss")
                if Decimal(str(wire_price)) != Decimal(str(expected_stop)):
                    raise RuntimeError("Wire stop price mismatch")
            elif wire.get("order_class") in ("oto", OrderClass.OTO):
                raise RuntimeError("Unexpected stop leg")

        self.api.permits.current = guard
        try:
            result = self.api.submit_order(order_data=request)
            return result.model_dump(mode="json") if hasattr(result, "model_dump") else dict(result)
        finally:
            self.api.permits.current = None

    # ------------------------------------------------------------------- quote
    def quote(self, symbol, risk=None, close=None):
        from alpaca.data.requests import StockLatestTradeRequest, StockLatestQuoteRequest
        from alpaca.data.enums import DataFeed

        # The Alpaca data SDK client is shared by the bounded premarket quote
        # pool; serialize access rather than assuming its HTTP session is safe.
        with self._quote_lock:
            client = self.strategy.broker.data_source._get_stock_client()
            quote = client.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=symbol, feed=DataFeed.IEX))[symbol]
            trade = None
            try:
                trade = client.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=symbol, feed=DataFeed.IEX))[symbol]
            except Exception:
                trade = None
        now = datetime.now(timezone.utc)
        age = (now - quote.timestamp).total_seconds()
        bid, ask = float(quote.bid_price), float(quote.ask_price)
        last = None
        last_timestamp = None
        last_age = None
        if trade is not None:
            last = float(trade.price)
            last_timestamp = trade.timestamp.isoformat()
            last_age = (now - trade.timestamp).total_seconds()
        max_age = risk.max_quote_age_seconds if risk is not None else 30
        if not all(math.isfinite(x) for x in (bid, ask)) or not 0 < bid <= ask or not 0 <= age <= max_age:
            raise ValueError("Stale or abnormal IEX quote")
        if (ask - bid) / bid > (risk.max_spread if risk is not None else 0.02):
            raise ValueError("Stale or abnormal IEX quote: spread")
        if last is not None and (not math.isfinite(last) or last <= 0):
            raise ValueError("Invalid last trade price")
        if last is not None and last_age is not None and last_age > max_age:
            # Do not let a stale last trade satisfy an execution trigger.
            last = None
            last_timestamp = None
            last_age = None
        return {
            "bid": bid,
            "ask": ask,
            "last": last,
            "timestamp": quote.timestamp.isoformat(),
            "last_timestamp": last_timestamp,
            "quote_age_seconds": age,
            "last_age_seconds": last_age,
            "source": "iex",
        }

    def cancel(self, cid):
        order = self.lookup(cid)
        if order:
            self.api.cancel_order_by_id(order["id"])

    def account_summary(self):
        snapshot = self.snapshot()
        return {
            "account_id_hash": snapshot["account_id_hash"],
            "equity": snapshot["equity"],
            "last_equity": snapshot["last_equity"],
            "cash": snapshot["cash"],
            "buying_power": snapshot["buying_power"],
            "non_margin_cash": snapshot["non_margin_cash"],
            "blocked": snapshot["blocked"],
            "positions": snapshot["position_details"],
            "quantities": snapshot["quantities"],
            "available_quantities": snapshot["available_quantities"],
            "open_orders": snapshot["open_orders"],
            "asof": snapshot["asof"],
        }
