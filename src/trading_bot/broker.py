from datetime import datetime, timezone
import math
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
        def _request(self, method, path, data=None, **kwargs):
            if str(self._base_url).rstrip("/") != PAPER_URL or kwargs.get("base_url", PAPER_URL) != PAPER_URL:
                raise RuntimeError("Paper endpoint invariant failed")
            from .market_data import execution_open, opening_minutes
            if str(method).upper()=="POST" and path.startswith("/orders") and not execution_open(datetime.now(timezone.utc)):
                raise RuntimeError("Order submission outside opening window")
            if str(method).upper()=="DELETE" and path.startswith("/orders"):
                minutes=opening_minutes(datetime.now(timezone.utc))
                if minutes is None or not 0<=minutes<30:
                    raise RuntimeError("Cancellation outside user operation window")
            return super()._request(method,path,data,**kwargs)
    client=PaperClient(keys["ALPACA_API_KEY"],keys["ALPACA_API_SECRET"],paper=True,url_override=PAPER_URL)
    client._retry=0
    client._session.mount("https://",BoundedAdapter(max_retries=0))
    cfg={"API_KEY":keys["ALPACA_API_KEY"],"API_SECRET":keys["ALPACA_API_SECRET"],"PAPER":True,"DATA_FEED":"iex"}
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.enums import DataFeed
    class IEXClient(StockHistoricalDataClient):
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
        def _get_dataframe_from_api(self, asset, length, **kwargs):
            from .market_data import regular_bars
            # Upstream only filters RTH for timestep == minute, and estimates
            # lookback as length/390 even for 15-minute bars. Overfetch + filter.
            df=super()._get_dataframe_from_api(asset,length=length if kwargs.get("timestep")=="day" else max(length*6,800),**kwargs)
            return (df.tail(length) if kwargs.get("timestep")=="day" else regular_bars(df).tail(length)) if df is not None else None
    data=RegularIEXData(cfg,delay=0,stock_client=UncachedIEX(),auto_adjust=False)
    broker=Alpaca(cfg,trading_client=client,data_source=data,connect_stream=False,start_orders_thread=start_orders_thread)
    return broker

class PaperGateway:
    def __init__(self, strategy):
        self.strategy=strategy
        self.api=strategy.broker.api

    def snapshot(self):
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        a=self.api.get_account(); ps=self.api.get_all_positions()
        orders=self.api.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN))
        return {"equity":float(a.equity),"last_equity":float(a.last_equity),"cash":float(a.cash),"buying_power":float(a.buying_power),"blocked":bool(a.trading_blocked or a.account_blocked),"positions":{p.symbol:float(p.market_value) for p in ps},"quantities":{p.symbol:float(p.qty) for p in ps},"open_orders":[o.model_dump(mode="json") for o in orders],"asof":datetime.now(timezone.utc).isoformat()}

    def is_open(self):
        return self.api.get_clock().is_open

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
        if not self.is_open():
            raise ValueError("Market closed before submission")
        from .market_data import execution_open
        if not execution_open(datetime.now(timezone.utc)):
            raise ValueError("Outside opening execution window")
        asset=self.api.get_asset(intent["symbol"])
        if intent["quantity"] % 1 and not asset.fractionable:
            raise ValueError("Asset does not support fractional orders")
        order=self.strategy.create_order(intent["symbol"],intent["quantity"],intent["side"],limit_price=intent["limit_price"],time_in_force="day",custom_params={"client_order_id":cid,"extended_hours":False})
        self.strategy.submit_order(order)

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
