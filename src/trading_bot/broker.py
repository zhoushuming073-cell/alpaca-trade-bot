from datetime import datetime, timezone
import math
import threading
import time
import json
from copy import deepcopy
from contextlib import contextmanager
from .safety import validate_order, IOCUnavailable
from .network import read_retry
from .settings import PAPER_URL

def build_broker(keys, start_orders_thread=None):
    from alpaca.trading.client import TradingClient
    from lumibot.brokers import Alpaca
    from lumibot.data_sources import AlpacaData
    from requests.adapters import HTTPAdapter
    class BoundedAdapter(HTTPAdapter):
        def send(self, request, **kwargs):
            kwargs["timeout"]=(3,5) if request.method=="DELETE" or "by_client_order_id" in request.url else (5,20)
            return super().send(request,**kwargs)
    class PaperClient(TradingClient):
        @contextmanager
        def fresh_reads(self):
            previous=getattr(self.permits,"fresh",False)
            self.permits.fresh=True
            try:
                yield
            finally:
                self.permits.fresh=previous

        def _request(self, method, path, data=None, **kwargs):
            if str(self._base_url).rstrip("/") != PAPER_URL or kwargs.get("base_url", PAPER_URL) != PAPER_URL:
                raise RuntimeError("Paper endpoint invariant failed")
            from .market_data import execution_open
            verb=str(method).upper()
            cacheable=verb=="GET" and path in {"/account","/positions","/orders","/clock"}
            cache_key=(path,json.dumps(data,sort_keys=True,default=str))
            if cacheable and not getattr(self.permits,"fresh",False):
                idle=getattr(self,"idle",lambda:False)()
                ttl=30 if not idle and execution_open(datetime.now(timezone.utc)) else 300
                with self.cache_lock:
                    cached=self.read_cache.get(cache_key)
                if cached and time.monotonic()-cached[0]<ttl:
                    return deepcopy(cached[1])
            if verb in {"POST", "PATCH", "PUT", "DELETE"}:
                if verb=="POST" and path=="/orders":
                    quantity=validate_order(data or {})
                    data=dict(data)
                    data["qty"]=str(quantity)  # SDK uses float internally; wire qty is an integer string
                    permit=getattr(self.permits,"current",None)
                    self.permits.current=None  # one use, even on a transport exception
                    if not permit:
                        raise RuntimeError("Direct order bypass forbidden: durable execution authorization required")
                    permit(data)
                    if not execution_open(datetime.now(timezone.utc)):
                        raise RuntimeError("Order submission outside opening window")
                elif verb=="DELETE" and path.startswith("/orders/"):
                    pass  # safety cancellation remains permitted outside the window
                else:
                    raise RuntimeError("Replacement, bulk submission and position liquidation endpoints disabled")
            operation=lambda: super(PaperClient,self)._request(method,path,data,**kwargs)
            result=read_retry(operation,getattr(self,"audit",None)) if verb=="GET" else operation()
            with self.cache_lock:
                if cacheable:
                    self.read_cache[cache_key]=(time.monotonic(),deepcopy(result))
                elif verb!="GET":
                    self.read_cache.clear()
            return result
    client=PaperClient(keys["ALPACA_API_KEY"],keys["ALPACA_API_SECRET"],paper=True,url_override=PAPER_URL)
    client._retry=0
    client.permits=threading.local()
    client.read_cache={}
    client.cache_lock=threading.RLock()
    client._session.mount("https://",BoundedAdapter(max_retries=0))
    cfg={"API_KEY":keys["ALPACA_API_KEY"],"API_SECRET":keys["ALPACA_API_SECRET"],"PAPER":True,"DATA_FEED":"iex"}
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.enums import DataFeed
    class IEXClient(StockHistoricalDataClient):
        def _request(self, method, path, data=None, **kwargs):
            operation=lambda: super(IEXClient,self)._request(method,path,data,**kwargs)
            return read_retry(operation,getattr(client,"audit",None)) if str(method).upper()=="GET" else operation()
        def get_stock_bars(self, request):
            request.feed=DataFeed.IEX
            if request.timeframe.unit.value=="Day":
                from alpaca.data.enums import Adjustment
                request.adjustment=Adjustment.SPLIT
            return super().get_stock_bars(request)
    stock=IEXClient(keys["ALPACA_API_KEY"],keys["ALPACA_API_SECRET"])
    stock._retry=0
    stock._session.mount("https://",BoundedAdapter(max_retries=0))
    class UncachedIEX:
        # Avoid upstream cache keyed before our explicit IEX feed is applied.
        def get_stock_bars(self,request): return stock.get_stock_bars(request)
        def get_stock_latest_quote(self,request): return stock.get_stock_latest_quote(request)
    class RegularIEXData(AlpacaData):
        def supports_native_timestep(self,timestep):
            return timestep in ("5 minutes", "5min", "5minute")
        def _get_dataframe_from_api(self, asset, length, **kwargs):
            from .market_data import regular_bars
            # Upstream only filters RTH for timestep == minute, and estimates
            # lookback as length/390 even for 15-minute bars. Overfetch + filter.
            df=super()._get_dataframe_from_api(asset,length=length if kwargs.get("timestep")=="day" else max(length*6,800),**kwargs)
            return (df.tail(length) if kwargs.get("timestep")=="day" else regular_bars(df).tail(length)) if df is not None else None
    data=RegularIEXData(cfg,delay=0,stock_client=UncachedIEX(),auto_adjust=False)
    class GuardedAlpaca(Alpaca):
        def _submit_order(self,order):
            payload={"symbol":order.asset.symbol,"qty":order.quantity,"side":order.side,
                     "limit_price":order.limit_price,"type":order.order_type,"time_in_force":order.time_in_force,
                     "order_class":order.order_class}
            payload.update(order.custom_params or {})
            validate_order(payload)
            if order.asset.asset_type!="stock":
                raise ValueError("Only US equity supported")
            return super()._submit_order(order)
        def _submit_orders(self,orders,**kwargs):
            raise ValueError("Bulk/multileg execution disabled; use audited single-order gateway")
    broker=GuardedAlpaca(cfg,trading_client=client,data_source=data,connect_stream=False,start_orders_thread=start_orders_thread)
    return broker

class PaperGateway:
    def __init__(self, strategy, store=None, ioc_confirmed=False, symbols=None):
        self.strategy=strategy
        self.api=strategy.broker.api
        self.store=store
        self.ioc_confirmed=ioc_confirmed
        from .settings import config
        self.symbols=set(config()[0] if symbols is None else symbols)
        if store:
            self.api.audit=store.event
            self.api.idle=lambda:store.paused() or bool(store.safety_reason())
            if not ioc_confirmed:
                store.block("IOC permission unverified: execution.ioc_permission_confirmed must be explicitly confirmed before trading")

    def snapshot(self):
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        with self.api.fresh_reads():
            a=self.api.get_account(); ps=self.api.get_all_positions()
            orders=self.api.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN,limit=500))
        if len(orders)>=500:
            raise ValueError("Open order result may be truncated; execution blocked")
        return {"equity":float(a.equity),"last_equity":float(a.last_equity),"cash":float(a.cash),"buying_power":float(a.buying_power),"blocked":bool(a.trading_blocked or a.account_blocked),"positions":{p.symbol:float(p.market_value) for p in ps},"quantities":{p.symbol:float(p.qty) for p in ps},"available_quantities":{p.symbol:float(p.qty_available if p.qty_available is not None else p.qty) for p in ps},"open_orders":[o.model_dump(mode="json") for o in orders],"asof":datetime.now(timezone.utc).isoformat()}

    def is_open(self):
        with self.api.fresh_reads():
            clock=self.api.get_clock()
        if abs((datetime.now(timezone.utc)-clock.timestamp).total_seconds())>5:
            raise ValueError("Local/broker clock skew exceeds five seconds")
        return clock.is_open

    def validate_assets(self,symbols):
        for s in symbols:
            a=self.api.get_asset(s)
            if not a.tradable or str(a.status.value)!="active" or a.asset_class.value!="us_equity":
                raise ValueError("Watchlist contains unavailable US equity: "+s)

    def lookup(self,cid):
        from alpaca.common.exceptions import APIError
        try:
            return self.api.get_order_by_client_id(cid).model_dump(mode="json")
        except APIError as exc:
            if exc.status_code==404:
                return None
            raise

    def submit(self,intent,cid):
        from decimal import Decimal
        from .risk_manager import sellable
        from alpaca.trading.requests import LimitOrderRequest
        validate_order(intent)
        if intent["symbol"] not in self.symbols:
            raise ValueError("Outside fixed watchlist")
        if not self.ioc_confirmed:
            raise IOCUnavailable("IOC permission unverified; no DAY fallback")
        if not self.store or not self.store.request(cid):
            raise ValueError("Missing durable order request")
        if self.store.paused() or self.store.safety_reason():
            raise ValueError("Execution paused or safety-locked")
        if not self.is_open():
            raise ValueError("Market closed before submission")
        from .market_data import execution_open
        if not execution_open(datetime.now(timezone.utc)):
            raise ValueError("Outside opening execution window")
        asset=self.api.get_asset(intent["symbol"])
        if not asset.tradable or asset.status.value!="active" or asset.asset_class.value!="us_equity":
            raise ValueError("Unavailable US equity")
        if intent["side"]=="sell" and intent["quantity"]>sellable(self.snapshot(),intent["symbol"]):
            raise ValueError("Fresh available whole shares insufficient")
        request=LimitOrderRequest(symbol=intent["symbol"],qty=intent["quantity"],side=intent["side"],
                                 limit_price=intent["limit_price"],time_in_force="ioc",client_order_id=cid,extended_hours=False)
        def guard(wire):
            stored=self.store.request(cid)
            if not stored or self.store.paused() or self.store.safety_reason():
                raise ValueError("Order authorization revoked")
            expected=stored["request"]
            if expected.get("valid_until") and datetime.now(timezone.utc)>datetime.fromisoformat(expected["valid_until"]):
                raise ValueError("Risk approval/quote expired before HTTP submission")
            if wire.get("client_order_id")!=cid or any(wire.get(k)!=expected[k] for k in ("symbol","side")) or Decimal(str(wire["qty"]))!=Decimal(str(expected["quantity"])) or Decimal(str(wire["limit_price"]))!=Decimal(str(expected["limit_price"])):
                raise ValueError("Wire order differs from risk-approved durable intent")
            if not self.store.dispatch_once(cid):
                raise ValueError("Order was already dispatched; query only, never repeat POST")
        # Synchronous SDK submission avoids LumiBot's asynchronous queue and hidden
        # exception handling. LumiBot still owns strategy scheduling and market data.
        self.api.permits.current=guard
        try:
            return self.api.submit_order(order_data=request).model_dump(mode="json")
        finally:
            self.api.permits.current=None

    def quote(self,symbol,risk,close):
        from alpaca.data.requests import StockLatestQuoteRequest
        from alpaca.data.enums import DataFeed
        client=self.strategy.broker.data_source._get_stock_client()
        q=client.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=symbol,feed=DataFeed.IEX))[symbol]
        age=(datetime.now(timezone.utc)-q.timestamp).total_seconds()
        bid,ask=float(q.bid_price),float(q.ask_price)
        if not all(math.isfinite(x) for x in (bid,ask)) or not 0<bid<=ask or not 0<=age<=risk.max_quote_age_seconds or (ask-bid)/bid>risk.max_spread or abs(ask/close-1)>risk.max_price_deviation:
            raise ValueError("Stale or abnormal IEX quote")
        return {"bid":bid,"ask":ask,"timestamp":q.timestamp.isoformat(),"source":"iex"}

    def cancel(self,cid):
        order=self.lookup(cid)
        if order:
            self.api.cancel_order_by_id(order["id"])
