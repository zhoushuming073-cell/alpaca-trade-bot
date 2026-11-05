import hashlib
import json
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from .market_data import window, validate_bars, indicators, execution_open, daily_history
from .risk_manager import plan
from .execution import reconcile, execute, monitor


class Engine:
    def __init__(self, store, broker, ai, symbols, policy, risk, data, mode="paper", clock=None, daily_data=None):
        self.store, self.broker, self.ai = store, broker, ai
        self.symbols, self.policy, self.risk, self.data = symbols, policy, risk, data
        self.mode, self.daily_data = mode, daily_data
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.last_monitor = None

    def tick(self):
        cycle, ticker, stage = None, None, "local_preflight"
        try:
            now = self.clock()
            if self.store.pending() and (self.last_monitor is None or (now-self.last_monitor).total_seconds() >= 10):
                self.last_monitor = now
                monitor(self.store, self.broker, cancel=True)
            end = window(now)
            if not execution_open(now):
                return "outside_window"
            if end is None:
                return "closed"
            if self.store.pending():
                return "pending"
            if self.store.paused() or self.store.safety_reason() or not self.policy.ready() or not self.symbols:
                return "disabled"
            cycle = end.isoformat()
            if not self.store.claim(cycle):
                return "duplicate"
            stage = "alpaca_snapshot"
            if not self.broker.is_open():
                self.store.finish(cycle, "closed")
                return "closed"
            account = reconcile(self.store, self.broker)
            if self.store.pending() or self.store.safety_reason():
                self.store.finish(cycle, "pending")
                return "pending"
            frames, metrics, daily, excluded = {}, {}, {}, {}
            for ticker in self.symbols:
                failures = []
                try:
                    frame = validate_bars(self.data(ticker), end, self.clock(), self.risk, self.policy.history_bars)
                    metric = indicators(frame, self.policy.indicators)
                except Exception as exc:
                    failures.append({"stage": "intraday_data", "type": type(exc).__name__, "reason": str(exc)})
                    self.store.error("intraday_data", exc, ticker=ticker, cycle=cycle, outcome="symbol_excluded",
                                     retried=bool(getattr(exc, "_bot_retries", 0)))
                if self.daily_data:
                    try:
                        daily[ticker] = daily_history(self.daily_data(ticker), self.clock()).reset_index().to_dict("records")
                    except Exception as exc:
                        failures.append({"stage": "daily_data", "type": type(exc).__name__, "reason": str(exc)})
                        self.store.error("daily_data", exc, ticker=ticker, cycle=cycle, outcome="symbol_excluded",
                                         retried=bool(getattr(exc, "_bot_retries", 0)))
                if failures:
                    excluded[ticker] = failures
                    daily.pop(ticker, None)
                else:
                    frames[ticker], metrics[ticker] = frame, metric
            ticker = None
            valid = list(frames)
            held_missing = [s for s, q in account["quantities"].items() if q > 0 and s not in valid]
            account["unpriced_holdings"] = held_missing
            payload = {"cycle": cycle, "symbols": valid, "fixed_watchlist": self.symbols, "excluded_symbols": excluded,
                       "frozen_holdings": held_missing, "bar_end": cycle,
                       "source": "synthetic" if self.mode == "dry-run" else "iex",
                       "strategy": self.policy.model_dump(), "risk": self.risk.model_dump(), "account": account,
                       "indicators": metrics, "bars": {s: df.reset_index().to_dict("records") for s, df in frames.items()},
                       "daily_bars": daily, "daily_history_scope": {"months": 6, "adjustment": "split", "intraday_adjustment": "raw"},
                       "unavailable_context": ["news", "sector_performance", "premarket_change"]}
            payload["config_hash"] = hashlib.sha256(json.dumps([self.symbols,self.policy.model_dump(),self.risk.model_dump()],sort_keys=True).encode()).hexdigest()
            payload["summary"] = {s: {"intraday_count": len(frames[s]), "daily_count": len(daily.get(s, [])),
                                      "first_bar": frames[s].index[0], "last_bar": frames[s].index[-1]} for s in valid}
            self.store.event("input", payload)
            if held_missing:
                self.store.event("held_data_alert", {"cycle": cycle, "symbols": held_missing,
                                                   "outcome": "freeze_affected_symbols_and_block_all_buys"})
            if not valid:
                self.store.finish(cycle, "skipped_no_valid_data")
                return "skipped_no_valid_data"
            self.check_fresh(end)
            stage = "deepseek_request"
            decisions, meta = self.ai.decide(payload)
            stage = "deepseek_schema"
            from .decision_schema import parse
            decisions = parse(json.dumps({"decisions": [d.model_dump() for d in decisions]}), valid)
            self.store.event("decision", {"cycle": cycle, "decisions": [d.model_dump() for d in decisions], **meta})
            for d in decisions:
                ticker = d.symbol
                if d.action == "HOLD":
                    self.store.event("risk_result", {"cycle": cycle, "ticker": ticker, "outcome": "hold"})
                    continue
                stage = "decision_freshness"
                self.check_fresh(end)
                stage = "alpaca_snapshot"
                account = reconcile(self.store, self.broker)
                account["unpriced_holdings"] = [s for s,q in account["quantities"].items() if q > 0 and s not in valid]
                if self.store.pending() or self.store.safety_reason():
                    break
                stage = "quote"
                try:
                    quote = self.broker.quote(ticker, self.risk, metrics[ticker]["close"])
                    self.store.event("quote", {"cycle": cycle, "symbol": ticker, **quote})
                    day = self.clock().astimezone(ZoneInfo("America/New_York")).date().isoformat()
                    stage = "risk"
                    intent = plan(d, account, quote, metrics[ticker], self.policy, self.risk, self.symbols,
                                  self.store.count(day), self.store.paused())
                except Exception as exc:
                    self.store.error(stage, exc, ticker=ticker, cycle=cycle, outcome="decision_rejected")
                    self.store.event("risk_rejected", {"cycle": cycle, "symbol": ticker, "reason": str(exc)})
                    continue
                self.store.event("risk_result", {"cycle": cycle, "ticker": ticker, "outcome": "approved" if intent else "below_one_share", "intent": intent})
                if intent:
                    expires=end+timedelta(seconds=self.risk.max_bar_age_seconds)
                    if quote.get("timestamp"):
                        expires=min(expires,datetime.fromisoformat(quote["timestamp"])+timedelta(seconds=self.risk.max_quote_age_seconds))
                    intent["valid_until"]=expires.isoformat()
                    stage = "order_submit"
                    self.check_fresh(end)
                    if not self.broker.is_open():
                        raise ValueError("Broker reports closed before submission")
                    execute(self.store, self.broker, cycle, day, intent)
                    stage = "post_trade_sync"
                    reconcile(self.store, self.broker)
            outcome = "halted" if self.store.safety_reason() or self.store.pending() else "complete"
            self.store.finish(cycle, outcome)
            return outcome
        except Exception as exc:
            self.store.error(getattr(exc, "stage", stage), exc, cycle=cycle, ticker=ticker,
                             retried=bool(getattr(exc, "_bot_retries", 0)), outcome="cycle_failed")
            if cycle:
                self.store.finish(cycle, "failed")
            return "failed"

    def check_fresh(self, end):
        now = self.clock()
        if self.store.paused() or self.store.safety_reason():
            raise ValueError("Paused or safety-locked")
        if not execution_open(now) or window(now) != end or not 0 <= (now-end).total_seconds() <= self.risk.max_bar_age_seconds:
            raise ValueError("Decision expired")
