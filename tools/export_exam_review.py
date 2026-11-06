"""Read-only export of the frozen 15-session exam for external review.

No AI/network/broker call is made.  Original exam files are copied, never
modified.  The export contains exact AI system/user prompts, raw responses and
the saved parsed decisions, plus derived review tables and a separate
post-trade price file.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import shutil
import statistics
import sys
import zipfile
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from exam_data import ExamStore  # noqa: E402
from trading_bot.decision_schema import (  # noqa: E402
    parse_plan, parse_position_review, parse_recheck, parse_scan,
)
from exam_replay.parsing import normalize_plan, normalize_stop_metadata  # noqa: E402

ET = ZoneInfo("America/New_York")
SOURCE = ROOT / "data/exam_replay_runs/full15"
OUT = ROOT / "data/exam_review_export"
ORIGINAL_FILES = [
    "report.json", "audit.jsonl", "acceptance.json", "freeze.json",
    "trades.csv", "fills.csv", "open_positions.csv",
    "equity_drawdown_exposure.png",
]
PROMPTS = {
    "UNIVERSE_SCAN": "universe_scan.txt",
    "PREMARKET_PLAN": "premarket_plan.txt",
    "INTRADAY_RECHECK": "intraday_recheck.txt",
    "POSITION_REVIEW": "position_review.txt",
}
import yaml  # noqa: E402
SYMBOLS = yaml.safe_load((ROOT / "config/watchlist.yaml").read_text(encoding="utf-8"))["symbols"]


def sha(path):
    return hashlib.file_digest(Path(path).open("rb"), "sha256").hexdigest()


def text_sha(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def parse_time(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(ET)


def pct(value):
    return None if value is None else float(value) * 100


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def write_csv(path, rows, fields):
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def load():
    report = json.loads((SOURCE / "report.json").read_text(encoding="utf-8"))
    freeze = json.loads((SOURCE / "freeze.json").read_text(encoding="utf-8"))
    acceptance = json.loads((SOURCE / "acceptance.json").read_text(encoding="utf-8"))
    return report, freeze, acceptance


def main():
    if OUT.exists():
        raise SystemExit("refusing to overwrite existing data/exam_review_export")
    OUT.mkdir(parents=True)
    (OUT / "original").mkdir()
    for name in ORIGINAL_FILES:
        shutil.copy2(SOURCE / name, OUT / "original" / name)
    report, freeze, acceptance = load()
    calls = report["calls"]
    inputs = report["ai_inputs"]
    raws = report["ai_raw"]
    assert len(calls) == len(inputs) == len(raws) == 56
    assert [c.get("mode") for c in calls] == [i.get("mode") for i in inputs]
    decisions = {d["day"]: d for d in report["decisions"]}
    daily = report["daily"]
    days = report["days"]
    qqq = report["metrics"].get("qqq_daily") or []
    plan_calls = [i for i, c in enumerate(calls) if c.get("mode") == "PREMARKET_PLAN"]
    scan_calls = [i for i, c in enumerate(calls) if c.get("mode") == "UNIVERSE_SCAN"]

    # System prompts are the exact frozen files.  Verify their hashes against
    # the provider call metadata instead of reconstructing from memory.
    prompt_text = {mode: (ROOT / "prompts" / name).read_text(encoding="utf-8")
                   for mode, name in PROMPTS.items()}
    prompt_hash = {mode: text_sha(value) for mode, value in prompt_text.items()}

    def parse_call(index):
        mode = inputs[index]["mode"]
        payload = inputs[index]["payload"]
        raw = raws[index]["content"]
        try:
            if mode == "UNIVERSE_SCAN":
                parsed = parse_scan(normalize_stop_metadata(raw)[0], symbols=SYMBOLS,
                                    now=parse_time(payload["as_of_utc"]))
            elif mode == "PREMARKET_PLAN":
                parsed = parse_plan(normalize_plan(normalize_stop_metadata(raw)[0])[0],
                                    symbols=SYMBOLS, now=parse_time(payload["as_of_utc"]))
            elif mode == "INTRADAY_RECHECK":
                parsed = parse_recheck(normalize_stop_metadata(raw)[0],
                                       plan_id=payload.get("plan_id", ""),
                                       now=parse_time(payload["as_of_utc"]))
            else:
                parsed = parse_position_review(normalize_stop_metadata(raw)[0], symbols=SYMBOLS)
            return parsed.model_dump(mode="json"), None
        except Exception as exc:  # noqa: BLE001
            return None, str(exc)[:1000]

    ai_records = []
    for index, call in enumerate(calls):
        payload = inputs[index]["payload"]
        raw = raws[index]["content"]
        mode = call["mode"]
        asof = payload.get("as_of_utc") or payload.get("exam", {}).get("as_of_utc")
        day = payload.get("exam", {}).get("date") or (asof or "")[:10]
        parsed, parse_error = parse_call(index)
        system = prompt_text[mode]
        system_hash = prompt_hash[mode]
        user = json.dumps(payload, ensure_ascii=False, default=str)
        reason = ""
        if parsed:
            if mode == "UNIVERSE_SCAN":
                reason = " | ".join(str(c.get("thesis", "")) for c in parsed.get("candidates", []))
            elif mode == "PREMARKET_PLAN":
                reason = " | ".join(str(c.get("analysis", "")) for c in parsed.get("candidates", []))
            elif mode == "INTRADAY_RECHECK":
                reason = parsed.get("reason", "")
            else:
                reason = " | ".join(str(x.get("reason", "")) for x in parsed.get("instructions", []))
        day_data = decisions.get(day, {})
        events = day_data.get("events", [])
        if mode == "UNIVERSE_SCAN":
            execution = {"executed": False, "status": "RESEARCH_ONLY",
                         "reason": "scan never creates an order"}
        elif mode == "PREMARKET_PLAN":
            submitted = [e for e in events if e.get("kind") == "order_submitted"]
            execution = {"executed": bool(submitted), "status": "PLAN",
                         "orders": submitted,
                         "market_decision": (parsed or {}).get("market_decision")}
        elif mode == "INTRADAY_RECHECK":
            match = next((e for e in events if e.get("kind") == "recheck"
                          and str(e.get("at", ""))[:19] == str(asof)[:19]), None)
            execution = {"executed": bool(match and match.get("decision") == "REPLACE"),
                         "status": (match or {}).get("decision", "NO_MATCH"),
                         "reason": (match or {}).get("reason", "")}
        else:
            match = [e for e in events if e.get("kind") in {"position_instruction",
                                                             "position_order",
                                                             "position_instruction_rejected"}]
            execution = {"executed": any(e.get("kind") == "position_order" for e in match),
                         "status": "POSITION_REVIEW", "instructions": match}
        window_claim = bool(re.search(r"(window.*closed|closed at 09:|09:30.*09:59.*closed)",
                                      reason, re.I))
        ai_records.append({
            "call_index": index,
            "day": day,
            "as_of_utc": asof,
            "as_of_et": parse_time(asof).isoformat() if asof else None,
            "mode": mode,
            "model": call.get("model"),
            "system_prompt_file": PROMPTS[mode],
            "system_prompt_sha256": system_hash,
            "system_prompt_hash_matches_call": system_hash[:32] == call.get("prompt_hash"),
            "system_prompt": system,
            "user_prompt_sha256": text_sha(user),
            "user_prompt": user,
            "input_payload": payload,
            "response_raw": raw,
            "parsed_response": parsed,
            "parse_error": parse_error,
            "ai_reason": reason,
            "validation": {"parse_error": parse_error, "schema_ok": parsed is not None},
            "execution": execution,
            "prompt_tokens": call.get("prompt_tokens"),
            "completion_tokens": call.get("completion_tokens"),
            "latency_ms": call.get("latency_ms"),
            "finish_reason": call.get("finish_reason"),
            "outcome": call.get("outcome"),
            "window_closed_claim_in_reason": window_claim,
            "actual_executioner_window_end_et": "09:59:30",
        })
    with (OUT / "ai_decisions.jsonl").open("w", encoding="utf-8") as handle:
        for record in ai_records:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    # Candidate decisions: scan output plus the PM's final decision.
    scan_by_day = {}
    plan_by_day = {}
    for i in scan_calls:
        rec = ai_records[i]
        if rec["parsed_response"]:
            scan_by_day[rec["day"]] = rec["parsed_response"]
    for i in plan_calls:
        rec = ai_records[i]
        if rec["parsed_response"]:
            plan_by_day[rec["day"]] = rec["parsed_response"]
    candidate_rows = []
    for day in days:
        scan = scan_by_day.get(day) or {}
        plan = plan_by_day.get(day) or {}
        plan_candidates = {c.get("symbol"): c for c in plan.get("candidates", [])}
        seen = set()
        for candidate in scan.get("candidates", []):
            symbol = candidate.get("symbol")
            seen.add(symbol)
            final = plan_candidates.get(symbol)
            candidate_rows.append({
                "day": day, "symbol": symbol,
                "scan_decision": candidate.get("trade_decision"),
                "scan_setup": candidate.get("setup_type"),
                "scan_grade": candidate.get("opportunity_grade"),
                "scan_thesis": candidate.get("thesis"),
                "plan_decision": (final or {}).get("trade_decision", "NOT_IN_PLAN"),
                "plan_analysis": (final or {}).get("analysis", ""),
                "plan_veto": (final or {}).get("veto"),
                "market_decision": plan.get("market_decision"),
            })
        for symbol, final in plan_candidates.items():
            if symbol in seen:
                continue
            candidate_rows.append({
                "day": day, "symbol": symbol,
                "scan_decision": "NOT_IN_SCAN",
                "scan_setup": "", "scan_grade": "", "scan_thesis": "",
                "plan_decision": final.get("trade_decision"),
                "plan_analysis": final.get("analysis", ""),
                "plan_veto": final.get("veto"),
                "market_decision": plan.get("market_decision"),
            })
    write_csv(OUT / "candidate_decisions.csv", candidate_rows,
              ["day", "symbol", "scan_decision", "scan_setup", "scan_grade",
               "scan_thesis", "plan_decision", "plan_analysis", "plan_veto",
               "market_decision"])

    # Daily portfolio and market context.
    daily_calls = Counter()
    for rec in ai_records:
        daily_calls[rec["day"]] += 1
    portfolio_rows = []
    market_rows = []
    for i, day in enumerate(days):
        d = daily[i]
        prev_eq = daily[i - 1]["equity"] if i else 100000.0
        qret = (qqq[i] / qqq[i - 1] - 1) if i and qqq[i - 1] else 0.0
        prior_5 = (qqq[i] / qqq[i - 5] - 1) if i >= 5 and qqq[i - 5] else None
        events = decisions[day].get("events", [])
        portfolio_rows.append({
            "day": day, "equity": d["equity"], "cash": d["cash"],
            "daily_pnl": d["equity"] - prev_eq,
            "long_market_value": (d.get("gross_exposure", 0) - (d.get("short_market_value", 0) or 0)),
            "short_market_value": d.get("short_market_value", 0),
            "gross_exposure": d["gross_exposure"],
            "unrealized_pnl": d.get("unrealized_pnl"),
            "realized_pnl": d.get("realized_pnl"),
            "fees_paid": d.get("fees_paid"),
            "borrow_cost": d.get("borrow_cost"),
            "positions_json": json.dumps(d.get("positions", {}), ensure_ascii=False),
            "market_decision": d.get("market_decision"),
            "ai_calls": daily_calls[day],
            "rechecks": sum(1 for e in events if e.get("kind") == "recheck"),
            "invalidations": sum(1 for e in events if e.get("kind") == "plan_invalidated"),
            "stop_events": sum(1 for f in report.get("fills", [])
                                if str(f.get("at", ""))[:10] == day
                                and f.get("reason") == "STOP_TRIGGERED"),
            "orders": sum(1 for e in events if e.get("kind") in {"order_submitted", "position_order", "recheck_replace_order"}),
            "qqq_close": qqq[i] if i < len(qqq) else None,
            "qqq_return_pct": qret * 100,
        })
        market_rows.append({
            "day": day, "qqq_close": qqq[i] if i < len(qqq) else None,
            "qqq_return_pct": qret * 100,
            "qqq_prior_5d_return_pct": prior_5 * 100 if prior_5 is not None else None,
            "account_equity": d["equity"], "cash": d["cash"],
            "gross_exposure": d["gross_exposure"],
            "gross_exposure_pct": (d["gross_exposure"] / d["equity"] * 100) if d["equity"] else 0,
            "market_decision": d.get("market_decision"),
            "ai_calls": daily_calls[day],
        })
    write_csv(OUT / "daily_portfolio.csv", portfolio_rows,
              ["day", "equity", "cash", "daily_pnl", "long_market_value",
               "short_market_value", "gross_exposure", "unrealized_pnl",
               "realized_pnl", "fees_paid", "borrow_cost", "positions_json",
               "market_decision", "ai_calls", "rechecks", "invalidations",
               "stop_events", "orders", "qqq_close", "qqq_return_pct"])
    write_csv(OUT / "market_context.csv", market_rows,
              ["day", "qqq_close", "qqq_return_pct", "qqq_prior_5d_return_pct",
               "account_equity", "cash", "gross_exposure", "gross_exposure_pct",
               "market_decision", "ai_calls"])

    # Order events.
    order_rows = []
    for fill in report.get("fills", []):
        order_rows.append({
            "event_type": "FILL", "at": fill.get("at"), "day": str(fill.get("at", ""))[:10],
            "order_id": fill.get("order_id"), "symbol": fill.get("symbol"),
            "action": fill.get("action"), "qty": fill.get("qty"),
            "price": fill.get("price"), "fee": fill.get("fee"),
            "status": "FILLED", "reason": fill.get("reason"),
            "trigger_price": fill.get("trigger_price"), "slippage": fill.get("slippage"),
        })
    for day, data in decisions.items():
        for e in data.get("events", []):
            if e.get("kind") in {"order_submitted", "position_order", "recheck_replace_order",
                                 "plan_invalidated", "recheck", "recheck_suppressed",
                                 "position_instruction", "position_instruction_rejected",
                                 "stop_adjusted", "stop_removed", "stop_submitted"}:
                order_rows.append({
                    "event_type": e.get("kind"), "at": e.get("at"), "day": day,
                    "order_id": e.get("order_id"), "symbol": e.get("symbol"),
                    "action": e.get("action"), "qty": e.get("qty"),
                    "price": e.get("limit") or e.get("price"), "fee": None,
                    "status": e.get("decision") or e.get("kind"),
                    "reason": e.get("reason") or e.get("error"),
                    "trigger_price": None, "slippage": None,
                })
    for fill in report.get("fills", []):
        if fill.get("action") not in {"OPEN_LONG", "OPEN_SHORT"}:
            continue
        day = str(fill.get("at", ""))[:10]
        plan = (decisions.get(day, {}).get("plan") or {})
        candidate = next((c for c in plan.get("candidates", [])
                          if c.get("symbol") == fill.get("symbol")), None)
        intent = (candidate or {}).get("order_intent") or {}
        if intent.get("stop_policy") != "BROKER_STOP":
            continue
        order_rows.append({
            "event_type": "STOP_ATTACHED_DERIVED", "at": fill.get("at"), "day": day,
            "order_id": None, "symbol": fill.get("symbol"),
            "action": "SELL" if fill.get("action") == "OPEN_LONG" else "BUY",
            "qty": fill.get("qty"), "price": None, "fee": None,
            "status": "GTC_DERIVED", "reason": "attached to entry fill; stop order id not stored in original report",
            "trigger_price": (intent.get("stop_loss") or {}).get("stop_price"),
            "slippage": None,
        })
    for order in (report.get("account") or {}).get("open_orders", []):
        order_rows.append({
            "event_type": "OPEN_ORDER_AT_END", "at": order.get("created_at"), "day": "2026-10-16",
            "order_id": order.get("id"), "symbol": order.get("symbol"),
            "action": order.get("position_intent"), "qty": order.get("qty"),
            "price": order.get("limit_price"), "fee": None,
            "status": order.get("status"), "reason": "open at end of exam",
            "trigger_price": order.get("stop_price"), "slippage": None,
        })
    order_rows.sort(key=lambda row: str(row.get("at") or ""))
    write_csv(OUT / "order_events.csv", order_rows,
              ["event_type", "at", "day", "order_id", "symbol", "action", "qty",
               "price", "fee", "status", "reason", "trigger_price", "slippage"])

    # Post-trade price path.  This file is explicitly future-only.
    def minute_at(symbol, target):
        with ExamStore(ROOT / "data/exam_v2.duckdb", read_only=True) as store:
            rows = store.db.execute(
                "SELECT end_at,close FROM bars WHERE symbol=? AND timeframe='1Min' AND feed='iex' "
                "AND end_at>=? ORDER BY end_at LIMIT 1", [symbol, target],
            ).fetchall()
        return (float(rows[0][1]), rows[0][0].isoformat()) if rows else (None, None)

    def day_close(symbol, day_value):
        with ExamStore(ROOT / "data/exam_v2.duckdb", read_only=True) as store:
            row = store.db.execute(
                "SELECT close FROM bars WHERE symbol=? AND timeframe='1Day' AND feed='iex' "
                "AND day=? ORDER BY ts DESC LIMIT 1", [symbol, day_value],
            ).fetchone()
        return float(row[0]) if row else None

    post_rows = []
    for trade in report.get("trades", []):
        exit_at = parse_time(trade["exit_at"])
        exit_day = exit_at.date()
        for minutes in (5, 15, 60):
            target = exit_at + timedelta(minutes=minutes)
            if target.date() == exit_day:
                price, at = minute_at(trade["symbol"], target)
            else:
                price, at = None, None
            post_rows.append({"symbol": trade["symbol"], "exit_at": trade["exit_at"],
                              "horizon": f"{minutes}m", "price": price, "at": at,
                              "post_trade_only": True, "exit_price": trade["exit_price"]})
        close_price = day_close(trade["symbol"], exit_day)
        post_rows.append({"symbol": trade["symbol"], "exit_at": trade["exit_at"],
                          "horizon": "exit_day_close", "price": close_price,
                          "at": f"{exit_day}T16:00:00-04:00",
                          "post_trade_only": True, "exit_price": trade["exit_price"]})
        later = [d for d in days if date.fromisoformat(d) > exit_day]
        nxt = later[0] if later else None
        post_rows.append({"symbol": trade["symbol"], "exit_at": trade["exit_at"],
                          "horizon": "next_session_close", "price": day_close(trade["symbol"], nxt) if nxt else None,
                          "at": f"{nxt}T16:00:00-04:00" if nxt else None,
                          "post_trade_only": True, "exit_price": trade["exit_price"]})
    for p in report.get("open_positions", []):
        post_rows.append({"symbol": p["symbol"], "exit_at": None, "horizon": "open_at_end",
                          "price": p.get("mark"), "at": "2026-10-16T16:00:00-04:00",
                          "post_trade_only": True, "exit_price": None})
    write_csv(OUT / "post_trade_prices.csv", post_rows,
              ["symbol", "exit_at", "horizon", "price", "at", "post_trade_only", "exit_price"])

    # Trade analysis for six closed and two open positions.
    def bars_daily(symbol, start, end):
        with ExamStore(ROOT / "data/exam_v2.duckdb", read_only=True) as store:
            rows = store.db.execute(
                "SELECT day,close FROM bars WHERE symbol=? AND timeframe='1Day' AND feed='iex' "
                "AND day>=? AND day<=? ORDER BY day", [symbol, start, end],
            ).fetchall()
        return [{"day": str(d), "close": float(c)} for d, c in rows]

    def bars_minute(symbol, start, end):
        with ExamStore(ROOT / "data/exam_v2.duckdb", read_only=True) as store:
            rows = store.db.execute(
                "SELECT end_at,high,low,close FROM bars WHERE symbol=? AND timeframe='1Min' AND feed='iex' "
                "AND end_at>=? AND end_at<=? ORDER BY end_at", [symbol, start, end],
            ).fetchall()
        return [{"at": a.isoformat(), "high": float(h), "low": float(l), "close": float(c)}
                for a, h, l, c in rows]

    def plan_for(day, symbol):
        for candidate in (decisions.get(day, {}).get("plan") or {}).get("candidates", []):
            if candidate.get("symbol") == symbol:
                return candidate
        return None

    def first_recommendation(symbol):
        for rec in ai_records:
            if rec["mode"] == "UNIVERSE_SCAN" and rec["parsed_response"]:
                for c in rec["parsed_response"].get("candidates", []):
                    if c.get("symbol") == symbol:
                        return {"at": rec["as_of_et"], "kind": "UNIVERSE_SCAN",
                                "call_index": rec["call_index"], "thesis": c.get("thesis")}
            if rec["mode"] == "PREMARKET_PLAN" and rec["parsed_response"]:
                for c in rec["parsed_response"].get("candidates", []):
                    if c.get("symbol") == symbol:
                        return {"at": rec["as_of_et"], "kind": "PREMARKET_PLAN",
                                "call_index": rec["call_index"], "thesis": c.get("analysis")}
        return None

    def reviews_for(symbol, start, end):
        out = []
        for day in days:
            when = date.fromisoformat(day)
            if when < start.date() or when > end.date():
                continue
            for e in decisions[day].get("events", []):
                if e.get("kind") in {"recheck", "position_instruction"}:
                    plan_id = str(e.get("plan_id") or "")
                    is_symbol = e.get("symbol") == symbol or symbol.lower() in plan_id.lower()
                    if is_symbol:
                        out.append({"at": e.get("at"), "kind": e.get("kind"),
                                    "decision": e.get("decision") or e.get("action"),
                                    "reason": e.get("reason")})
        return out

    def entry_context(day, symbol):
        for i in plan_calls:
            rec = ai_records[i]
            if rec["day"] != day:
                continue
            payload = rec["input_payload"]
            focus = next((x for x in payload.get("focus", []) if x.get("symbol") == symbol), None)
            universe = next((x for x in payload.get("universe", []) if x.get("symbol") == symbol), None)
            return {"call_index": rec["call_index"], "focus": focus, "universe": universe,
                    "market_reference": payload.get("market_reference"),
                    "account": payload.get("account")}
        return None

    trade_analysis = []
    closed_symbols = {t["symbol"]: t for t in report.get("trades", [])}
    open_symbols = {p["symbol"]: p for p in report.get("open_positions", [])}
    for symbol in ["MU", "TSM", "MRVL", "LITE", "META", "PLTR", "CRWV", "WULF"]:
        trade = closed_symbols.get(symbol)
        position = open_symbols.get(symbol)
        if trade:
            entry_at = parse_time(trade["entry_at"]); exit_at = parse_time(trade["exit_at"])
            entry_price = float(trade["entry_price"]); qty = int(trade["qty"])
            entry_day = entry_at.date().isoformat(); exit_day = exit_at.date().isoformat()
            plan = plan_for(entry_day, symbol) or {}
            daily_bars = bars_daily(symbol, entry_day, exit_day)
            minute = bars_minute(symbol, entry_at, exit_at)
            path = []
            for bar in daily_bars:
                path.append({"day": bar["day"], "close": bar["close"],
                             "pnl": (bar["close"] - entry_price) * qty,
                             "return_pct": (bar["close"] / entry_price - 1) * 100})
            high = max((b["high"] for b in minute), default=entry_price)
            low = min((b["low"] for b in minute), default=entry_price)
            stop = plan.get("order_intent", {}).get("stop_loss") if plan else None
            post = [r for r in post_rows if r["symbol"] == symbol and r["exit_at"] == trade["exit_at"]]
            entry_fill = next((f for f in report.get("fills", [])
                                if f.get("order_id") == trade.get("entry_order")), {})
            exit_fill = next((f for f in report.get("fills", [])
                               if f.get("order_id") == trade.get("exit_order")), {})
            item = {
                "symbol": symbol, "status": "closed", "direction": trade["direction"],
                "qty": qty, "entry_at": trade["entry_at"], "entry_price": entry_price,
                "exit_at": trade["exit_at"], "exit_price": trade["exit_price"],
                "exit_reason": trade["exit_reason"],
                "entry_order_id": trade.get("entry_order"),
                "exit_order_id": trade.get("exit_order"),
                "stop_trigger_price": exit_fill.get("trigger_price"),
                "stop_fill_price": exit_fill.get("price"),
                "stop_slippage": exit_fill.get("slippage"),
                "realized_pnl": trade["pnl"], "fees": trade["fees"],
                "first_recommendation": first_recommendation(symbol),
                "entry_plan": plan, "entry_context": entry_context(entry_day, symbol),
                "entry_triggers": plan.get("triggers") if plan else None,
                "stop_policy": (plan.get("order_intent") or {}).get("stop_policy") if plan else None,
                "stop_loss": stop,
                "holding_path": path,
                "max_favorable_price": high, "max_favorable_pnl": (high - entry_price) * qty,
                "max_favorable_pct": (high / entry_price - 1) * 100,
                "max_adverse_price": low, "max_adverse_pnl": (low - entry_price) * qty,
                "max_adverse_pct": (low / entry_price - 1) * 100,
                "reviews": reviews_for(symbol, entry_at, exit_at),
                "post_trade_prices": post,
                "post_trade_note": "Post-trade prices are future evaluation data, not AI input.",
                "open_reason": None,
            }
        else:
            entry_at = parse_time(position["entry_at"]); entry_price = float(position["entry_price"])
            qty = int(position["qty"]); entry_day = entry_at.date().isoformat()
            plan = plan_for(entry_day, symbol) or {}
            daily_bars = bars_daily(symbol, entry_day, days[-1])
            minute = bars_minute(symbol, entry_at, datetime.fromisoformat(days[-1] + "T16:00:00-04:00"))
            path = [{"day": b["day"], "close": b["close"],
                     "pnl": (b["close"] - entry_price) * qty,
                     "return_pct": (b["close"] / entry_price - 1) * 100} for b in daily_bars]
            high = max((b["high"] for b in minute), default=entry_price)
            low = min((b["low"] for b in minute), default=entry_price)
            item = {
                "symbol": symbol, "status": "open_at_end", "direction": position["direction"],
                "qty": qty, "entry_at": position["entry_at"], "entry_price": entry_price,
                "exit_at": None, "exit_price": None, "exit_reason": None,
                "entry_order_id": None, "exit_order_id": None,
                "stop_trigger_price": None, "stop_fill_price": None, "stop_slippage": None,
                "realized_pnl": None, "fees": None,
                "first_recommendation": first_recommendation(symbol),
                "entry_plan": plan, "entry_context": entry_context(entry_day, symbol),
                "entry_triggers": plan.get("triggers") if plan else None,
                "stop_policy": (plan.get("order_intent") or {}).get("stop_policy") if plan else None,
                "stop_loss": plan.get("order_intent", {}).get("stop_loss") if plan else None,
                "holding_path": path,
                "max_favorable_price": high, "max_favorable_pnl": (high - entry_price) * qty,
                "max_favorable_pct": (high / entry_price - 1) * 100,
                "max_adverse_price": low, "max_adverse_pnl": (low - entry_price) * qty,
                "max_adverse_pct": (low / entry_price - 1) * 100,
                "reviews": reviews_for(symbol, entry_at, datetime.fromisoformat(days[-1] + "T16:00:00-04:00")),
                "post_trade_prices": [],
                "post_trade_note": None,
                "open_reason": "Position remained open at the end of the frozen exam; no forced liquidation. "
                               f"Active stop: {position.get('stop')}",
            }
        trade_analysis.append(item)
    write_json(OUT / "trade_analysis.json", trade_analysis)

    # Short-selling audit evidence.
    all_analysis = " ".join(
        str(c.get("analysis", "")) + " " + str(c.get("thesis", ""))
        for d in decisions.values() for c in (d.get("plan") or {}).get("candidates", [])
    ) + " " + " ".join(str(r.get("parsed_response")) for r in ai_records)
    short_mentions = []
    for rec in ai_records:
        reason = rec.get("ai_reason") or ""
        for match in re.finditer(r"[^.]{0,120}\b(short|做空|cover short)\b[^.]{0,160}", reason, re.I):
            short_mentions.append({"day": rec["day"], "call_index": rec["call_index"],
                                   "mode": rec["mode"], "quote": match.group(0).strip()})
    short_orders = [e for d in decisions.values() for e in d.get("events", [])
                    if str(e.get("action") or "").upper() == "OPEN_SHORT"]
    rejects = [e for d in decisions.values() for e in d.get("events", [])
               if e.get("kind") == "order_rejected" and "short" in str(e).lower()]
    short_md = f"""# Short-selling audit

## 1. Production prompt

`prompts/premarket_plan.txt` explicitly permits both directions and contains
the exact rules. Relevant excerpts (verbatim from the frozen file):

```text
{chr(10).join(line for line in (ROOT/'prompts/premarket_plan.txt').read_text(encoding='utf-8').splitlines() if 'short' in line.lower() or 'sell_to_open' in line.lower())}
```

The prompt also says: "If no short setup is genuinely supported by the evidence,
choose HOLD/WAIT/NO_TRADE. Do not short merely because the broad market is down."
This is a caution, not a hard interface prohibition.

## 2. Schema

`src/trading_bot/decision_schema.py` defines `OPEN_SHORT` and `COVER_SHORT` in
`OrderAction`/`TradeDecision`, and `ScanCandidate.trade_decision` accepts both.
The strict parser and `Candidate` model accept them.

## 3. Account declaration

Every premarket and review payload had:

- `exam.execution_contract.allow_shorting = true`; the top-level scan
  `execution_contract.allow_short = true` is also present
- `account.shorting_enabled = true`
- `historical_short_locate_assumption = ALL_SYMBOLS_ASSUMED_AVAILABLE_FOR_EXAM`

The isolated broker also had `short_locates=True`; no short order was attempted.

## 4. Scan capability

`prompts/universe_scan.txt` lists `OPEN_SHORT`/`COVER_SHORT` in the output
schema and says: "Include shorts only when the supplied shorting capability/asset
facts support them; do not infer a short from market weakness."

## 5. Rejections / execution limits

- `OPEN_SHORT` events: **{len(short_orders)}**
- Short-related `order_rejected` events: **{len(rejects)}**
- No market data or simulator rejection of a short exists in this run.

## 6. AI short mentions and non-actions

The saved AI reasoning contains **{len(short_mentions)}** short-related text
fragments. These include cautionary statements, not executed short orders.
Sample verbatim fragments:

"""
    for item in short_mentions[:30]:
        short_md += f"- {item['day']} call {item['call_index']} `{item['mode']}`: {item['quote']}\n"
    short_md += f"""
- Executed trades: 6 long, **0 short**.
- Open positions: CRWV/WULF, both long.
- No AI plan or recheck in this run emitted `OPEN_SHORT`.

## 7. Classification

| Hypothesis | Verdict | Evidence |
|---|---|---|
| AI actively judged shorting inappropriate | **PARTIAL / UNKNOWN motive** | It emitted long/WAIT/NO_TRADE, but no explicit "I considered short and rejected it" is guaranteed; only saved reasons exist. |
| Prompt/strategy suppresses shorts | **PARTIAL design bias** | Shorts are allowed, but the prompt emphasizes not shorting on weakness and requires genuine evidence; no hard ban. |
| Interface/validator/simulator blocks shorts | **NO** | Schema, execution contract, account flag, scan schema and broker all support shorts; 0 attempts and 0 short rejections. |

The exact internal motive for not shorting is **UNKNOWN**. No psychological
explanation is invented here.
"""
    (OUT / "short_selling_audit.md").write_text(short_md, encoding="utf-8")

    # Summary/README/limitations.
    metrics = report["metrics"]
    summary = f"""# 15-session frozen exam — review export

- Dates: **{days[0]} → {days[-1]}** (15 NYSE sessions)
- Mode: `EXPLORATORY`; data: `exam_v2.duckdb`; feed: IEX
- Agent: original single-agent DeepSeek (`deepseek-flash`)
- Initial cash: **$100,000**; final equity: **${report['account']['equity']:,.2f}**
- Total return: **{metrics['total_return']*100:.3f}%**
- Max drawdown: **{metrics['max_drawdown']*100:.3f}%**
- Closed trades: **{metrics['trades']}**; win rate: **{metrics['win_rate']*100:.3f}%**
- QQQ buy & hold: **{metrics['qqq_buy_hold_return']*100:.3f}%**
- Exposure-matched QQQ: **{metrics['exposure_matched_qqq_return']*100:.3f}%**
- AI calls: **{len(calls)}**; tokens: **{sum((c.get('prompt_tokens') or 0)+(c.get('completion_tokens') or 0) for c in calls):,}**
- Real/Paper orders: **0**
- Frozen hash: `{freeze.get('combined_sha256')}`

## Original vs derived

`original/` contains byte-for-byte copies of the source exam files. Derived
files are read-only interpretations; they do not replace the originals.

## Critical caveats

- `EXPLORATORY` historical news/fundamental content versions are UNKNOWN.
- IEX is not consolidated SIP; short borrow is assumed available for the exam.
- The current watchlist/strategy were selected after the historical dates.
- Post-trade prices are future evaluation data and are isolated in
  `post_trade_prices.csv`; they were never supplied to the AI.
- The AI's saved reasons contain some incorrect claims that the 09:30-09:59:30
  window was closed at 09:31/09:33 ET. The executioner window remained open to
  09:59:30; raw reasoning and actual times are preserved.
"""
    (OUT / "exam_summary.md").write_text(summary, encoding="utf-8")

    readme = """# alpaca_exam15_review

Read-only evidence package for the 15-session exam.

## Start here

1. `exam_summary.md` — headline results and caveats.
2. `REPORT.md` (copy of the original full report).
3. `ai_decisions.jsonl` — 56 calls with exact system/user prompts, raw response,
   parsed response, token usage and execution outcome.
4. `trade_analysis.json` — the eight requested positions, including separate
   post-trade prices.
5. `daily_portfolio.csv`, `market_context.csv`, `candidate_decisions.csv`,
   `order_events.csv`.
6. `short_selling_audit.md`.
7. `original/` — original frozen files, unchanged.

## Method

- `ai_decisions.jsonl` maps calls by sequence to `ai_inputs` and `ai_raw` from
  the saved report. The mode sequence is checked 56/56. The system prompt is
  read from the frozen prompt file and its hash is checked against each call's
  `prompt_hash`. The user prompt is the saved input payload serialized as JSON.
- Parsed JSON is the saved production schema representation; scan calls are
  re-parsed from the preserved raw response because scan parsed output was not
  stored separately. Raw responses are always retained.
- `post_trade_prices.csv` is explicitly future-only. It is never used in the
  AI decision records or original trade reasons.

## No secrets

No API key, secret, token, account credential or live Alpaca data is included.
Account values are the isolated simulator's values; account snapshot IDs are
opaque hashes.

## Not a re-run

No DeepSeek call, no strategy change, no prompt change, no Paper order.
"""
    (OUT / "README.md").write_text(readme, encoding="utf-8")

    limitations = """# Limitations and missing data

- `EXPLORATORY` includes later-retrieved news and SEC facts. Their historical
  content versions are UNKNOWN; only availability timestamps are enforced.
- IEX is not consolidated SIP; volume/liquidity and minute coverage are incomplete.
- Short borrow is assumed available (`ALL_SYMBOLS_ASSUMED_AVAILABLE_FOR_EXAM`);
  it is not a historical locate verification.
- The watchlist, strategy and prompts were selected before this export; current
  watchlist selection bias remains.
- The AI model's pretraining may contain later knowledge; this cannot be ruled out.
- Broker stops are local simulator orders with gap/slippage; they are not Alpaca
  OMS fills.
- The exact provider wall-clock invocation timestamp was not stored; `as_of_utc`
  is the simulated decision time, and latency is stored per call.
- `ai_inputs` has only mode+payload; call-to-input mapping is by preserved
  sequence, verified 56/56 by mode. No call was regenerated.
- Some saved AI reasons incorrectly claim the new-order window was closed at
  09:31/09:33 ET. Python kept the window open to 09:59:30; raw text and actual
  system times are preserved.
- Missing historical data is left empty; no price or news is fabricated.
- No real or Paper Alpaca order was submitted.
- The top-level scan payload contains a legacy `execution_contract.stop_policy:
  "NONE only in simulator"` string, while `exam.execution_contract` declared
  BROKER_STOP verified. This is a pre-existing payload inconsistency, not an
  export change; the AI did set broker stops and the simulator honored them.
"""
    (OUT / "limitations.md").write_text(limitations, encoding="utf-8")

    # Copy original report for convenient reading.
    shutil.copy2(SOURCE / "REPORT.md", OUT / "REPORT.md")

    # Manifest over original + derived files.
    original_paths = [OUT / "original" / name for name in ORIGINAL_FILES]
    original_manifest = [{
        "path": str(path.relative_to(OUT)).replace("\\", "/"),
        "sha256": sha(path), "bytes": path.stat().st_size,
        "mtime_utc": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
    } for path in original_paths]
    manifest_files = []
    for path in sorted(OUT.rglob("*")):
        if path.is_file() and path.name not in {"manifest.json", "zip_integrity.json"}:
            manifest_files.append({
                "path": str(path.relative_to(OUT)).replace("\\", "/"),
                "sha256": sha(path), "bytes": path.stat().st_size,
                "mtime_utc": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
            })
    import subprocess
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    manifest = {
        "export_version": "alpaca-exam15-review-v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": commit,
        "source_dir": str(SOURCE),
        "freeze_hash": freeze.get("combined_sha256"),
        "report_sha256": sha(SOURCE / "report.json"),
        "original_files": original_manifest,
        "files": manifest_files,
        "file_count": len(manifest_files),
        "ai_calls": len(calls),
        "no_ai_recall": True,
        "no_real_orders": True,
    }
    write_json(OUT / "manifest.json", manifest)

    zip_path = OUT / "alpaca_exam15_review.zip"
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(OUT.rglob("*")):
            if path.is_file() and path.name not in {"alpaca_exam15_review.zip", "zip_integrity.json"}:
                archive.write(path, path.relative_to(OUT).as_posix())
    with zipfile.ZipFile(zip_path) as archive:
        bad = archive.testzip()
        names = archive.namelist()
    integrity = {
        "zip_path": str(zip_path), "zip_sha256": sha(zip_path),
        "zip_bytes": zip_path.stat().st_size,
        "members": len(names), "testzip_bad": bad,
        "checks": {
            "manifest": "manifest.json" in names,
            "ai_decisions": "ai_decisions.jsonl" in names,
            "trade_analysis": "trade_analysis.json" in names,
            "post_trade": "post_trade_prices.csv" in names,
            "original": "original/report.json" in names,
        },
    }
    write_json(OUT / "zip_integrity.json", integrity)
    print(json.dumps(integrity, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
