from datetime import datetime, timezone
from lumibot.strategies import Strategy
from .settings import config, credentials
from .broker import PaperGateway
from .deepseek_client import DeepSeek
from .engine import Engine

class PaperStrategy(Strategy):
    def initialize(self, store):
        self.sleeptime="5S"
        self.set_market("NYSE")
        self.minutes_before_closing=0
        self.minutes_before_opening=0
        symbols,policy,risk=config()
        self.daily_cache={}
        self.gateway=PaperGateway(self)
        self.gateway.validate_assets(symbols)
        self.engine=Engine(store,self.gateway,DeepSeek(credentials()["DEEPSEEK_API_KEY"]),symbols,policy,risk,
                           lambda s:self.get_historical_prices(s,policy.history_bars+2,timestep="5 minutes",include_after_hours=False).df,
                           daily_data=self.daily_history)

        from .watchdog import CutoffWatchdog
        self.watchdog=CutoffWatchdog(store.path,self.gateway)
        self.watchdog.start()

    def on_trading_iteration(self):
        self.engine.tick()

    def after_market_closes(self):
        from .execution import reconcile
        try:
            reconcile(self.engine.store,self.gateway)
        except Exception as exc:
            self.engine.store.event("error",{"type":type(exc).__name__})

    def on_abrupt_closing(self):
        self.watchdog.stop()
        self.engine.store.event("stopped",{"reason":"operator_shutdown"})

    def daily_history(self,symbol):
        from zoneinfo import ZoneInfo
        day=datetime.now(ZoneInfo("America/New_York")).date()
        key=(day,symbol)
        if key not in self.daily_cache:
            self.daily_cache={k:v for k,v in self.daily_cache.items() if k[0]==day}
            self.daily_cache[key]=self.get_historical_prices(symbol,160,timestep="day").df
        return self.daily_cache[key]
