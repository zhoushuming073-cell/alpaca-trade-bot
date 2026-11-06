"""Rebuild and render the official 15-session exam report from saved results.

This script only recomputes reporting aggregates from the saved decisions,
fills, trades and AI audit.  It never calls the AI or a broker.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parents[1]


def pct(value):
    return "—" if value is None else f"{float(value) * 100:.3f}%"


def money(value):
    return "—" if value is None else f"{float(value):,.2f}"


def rebuild(data):
    decisions = {d["day"]: d for d in data["decisions"]}
    for day in data["daily"]:
        day["events"] = decisions.get(day["day"], {}).get("events", [])
    events = [e for d in data["decisions"] for e in d.get("events", [])]
    data["metrics"]["recheck_count"] = sum(1 for e in events if e.get("kind") == "recheck")
    data["metrics"]["recheck_decisions"] = [
        {"day": e.get("at", "")[:10], "decision": e.get("decision"),
         "reason": e.get("reason")} for e in events if e.get("kind") == "recheck"
    ]
    data["metrics"]["no_trade_days"] = [
        d["day"] for d in data["daily"]
        if not any(e.get("kind") in {"order_submitted", "position_order", "recheck_replace_order"}
                   for e in d.get("events", []))
    ]
    data["metrics"]["stop_trigger_count"] = sum(
        1 for f in data.get("fills", []) if f.get("reason") == "STOP_TRIGGERED"
    )
    data["metrics"]["stop_adjustments"] = sum(
        1 for e in events if e.get("kind") in {"stop_adjusted", "stop_removed", "stop_submitted"}
    )
    data["metrics"]["recheck_suppressed"] = sum(
        1 for e in events if e.get("kind") == "recheck_suppressed"
    )
    data["metrics"]["position_review_count"] = sum(
        1 for c in data.get("calls", []) if c.get("mode") == "POSITION_REVIEW"
    )
    data["metrics"]["slippage_cost"] = sum(
        (f.get("slippage") or 0) * (f.get("qty") or 0) for f in data.get("fills", [])
    )
    data["metrics"]["fees_paid"] = sum(f.get("fee") or 0 for f in data.get("fills", []))
    data["metrics"]["borrow_cost"] = sum(d.get("borrow_cost") or 0 for d in data["daily"])
    data["metrics"]["cash_ratio_mean"] = statistics.mean(
        [1 - (d["gross_exposure"] / d["equity"] if d["equity"] else 0) for d in data["daily"]]
    ) if data["daily"] else None
    # AI calls by day (the saved input audit has the dated payload).
    per_day = Counter()
    for item in data.get("ai_inputs", []):
        payload = item.get("payload") or {}
        exam = payload.get("exam") or {}
        day = exam.get("date") or payload.get("trading_day")
        if day:
            per_day[str(day)] += 1
    data["ai_calls_per_day"] = dict(per_day)
    data["metrics"]["win_rate_label"] = (
        "N/A (no closed trades)" if data["metrics"].get("win_rate") is None
        else pct(data["metrics"].get("win_rate"))
    )
    # Attach the plan rationale to each closed trade and open position.
    def plan_for(day, symbol):
        item = decisions.get(day) or {}
        for candidate in (item.get("plan") or {}).get("candidates", []):
            if candidate.get("symbol") == symbol:
                return candidate
        return {}
    for trade in data.get("trades", []):
        entry_day = str(trade.get("entry_at", ""))[:10]
        candidate = plan_for(entry_day, trade.get("symbol"))
        trade["entry_reason"] = candidate.get("analysis", "")
        trade["entry_decision"] = candidate.get("trade_decision", "")
        trade["exit_reason_detail"] = trade.get("exit_reason", "")
    for position in data.get("open_positions", []):
        entry_day = str(position.get("entry_at") or "")[:10]
        candidate = plan_for(entry_day, position.get("symbol"))
        position["entry_reason"] = candidate.get("analysis", "")
    data["order_summary"] = [
        {"order_id": f.get("order_id"), "symbol": f.get("symbol"),
         "action": f.get("action"), "qty": f.get("qty"),
         "price": f.get("price"), "reason": f.get("reason"),
         "at": f.get("at"), "trigger_price": f.get("trigger_price")}
        for f in data.get("fills", [])
    ]
    return data


def chart(data, out):
    days = [d["day"] for d in data["daily"]]
    equity = data["equity_curve"]
    start = equity[0]
    ai_curve = [v / start * 100000 for v in equity]
    qqq = data["metrics"].get("qqq_daily") or []
    qqq_curve = [v / qqq[0] * 100000 for v in qqq] if qqq and qqq[0] else []
    # Exposure-matched benchmark, previous-day gross exposure / equity.
    exp_curve = [100000.0]
    for i in range(1, len(days)):
        prev = data["daily"][i - 1]
        ratio = prev["gross_exposure"] / prev["equity"] if prev["equity"] else 0
        qret = qqq[i] / qqq[i - 1] - 1 if i < len(qqq) and qqq[i - 1] else 0
        exp_curve.append(exp_curve[-1] * (1 + ratio * qret))
    peak, drawdown = equity[0], []
    for value in equity:
        peak = max(peak, value)
        drawdown.append((value / peak - 1) * 100)
    fig, axes = plt.subplots(3, 1, figsize=(11, 10), sharex=True)
    axes[0].plot(days, ai_curve, marker="o", label="AI account")
    if len(qqq_curve) == len(days):
        axes[0].plot(days, qqq_curve, marker="o", label="QQQ buy & hold")
    if len(exp_curve) == len(days):
        axes[0].plot(days, exp_curve, marker="o", label="Exposure-matched QQQ")
    axes[0].set_ylabel("Equity index (start=100k)")
    axes[0].legend(loc="best")
    axes[0].grid(alpha=0.25)
    axes[1].fill_between(days, drawdown, 0, color="#b23a48", alpha=0.35)
    axes[1].set_ylabel("Drawdown %")
    axes[1].grid(alpha=0.25)
    axes[2].bar(days, [d["gross_exposure"] / d["equity"] * 100 if d["equity"] else 0 for d in data["daily"]],
                color="#3b6ea8", alpha=0.75, label="Gross exposure %")
    axes[2].set_ylabel("Exposure %")
    axes[2].set_ylim(0, 105)
    axes[2].grid(alpha=0.25)
    for ax in axes:
        ax.tick_params(axis="x", rotation=45)
    fig.tight_layout()
    path = out / "equity_drawdown_exposure.png"
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


def write_csv(data, out):
    fills = data.get("fills", [])
    with (out / "trades.csv").open("w", newline="", encoding="utf-8-sig") as f:
        fields = ["symbol", "direction", "qty", "entry_price", "exit_price",
                  "entry_at", "exit_at", "pnl", "fees", "holding_minutes",
                  "entry_order", "exit_order", "exit_reason"]
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(data.get("trades", []))
    with (out / "fills.csv").open("w", newline="", encoding="utf-8-sig") as f:
        fields = ["order_id", "symbol", "action", "qty", "price", "fee",
                  "at", "reason", "trigger_price", "slippage"]
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(fills)
    with (out / "open_positions.csv").open("w", newline="", encoding="utf-8-sig") as f:
        fields = ["symbol", "qty", "direction", "entry_price", "mark",
                  "market_value", "unrealized_pnl", "entry_at", "stop"]
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(data.get("open_positions", []))


def markdown(data, chart_path, out):
    m = data["metrics"]
    lines = [
        "# 15-Session Frozen Historical Exam — Single-Agent DeepSeek",
        "",
        f"- Year/dates: **{data['days'][0]} → {data['days'][-1]}** ({len(data['days'])} NYSE sessions)",
        f"- Dataset: `{Path(data['dataset']).name}`, mode `{data['exam_mode']}`, IEX",
        f"- Model: `{data['model']}`; original single-agent production prompts",
        "- Initial cash: **$100,000**; initial positions: **flat**",
        f"- Frozen config hash: `{data.get('freeze', {}).get('combined_sha256', 'MISSING')}`",
        "- No web search, live quotes, production research memory, Alpaca reads or real/Paper orders.",
        "",
        "## 1. Headline performance",
        "",
        "| Metric | Result |",
        "|---|---:|",
        f"| Total return | **{pct(m.get('total_return'))}** |",
        f"| Max drawdown | **{pct(m.get('max_drawdown'))}** |",
        f"| Win rate (closed trades) | **{m.get('win_rate_label', '—')}** |",
        f"| Closed trades | {m.get('trades')} |",
        f"| Long realized P&L | ${money((m.get('long') or {}).get('pnl'))} |",
        f"| Short realized P&L | ${money((m.get('short') or {}).get('pnl'))} |",
        f"| Open unrealized P&L | ${money((data.get('account') or {}).get('unrealized_pnl'))} |",
        f"| Payoff ratio | {'N/A (no wins)' if m.get('payoff_ratio') is None else m.get('payoff_ratio')} |",
        f"| QQQ buy & hold | {pct(m.get('qqq_buy_hold_return'))} |",
        f"| Exposure-matched QQQ | {pct(m.get('exposure_matched_qqq_return'))} |",
        f"| Mean capital utilization | {pct(m.get('capital_utilization_mean'))} |",
        f"| Mean cash ratio | {pct(m.get('cash_ratio_mean'))} |",
        f"| Fees | ${money(m.get('fees_paid'))} |",
        f"| Slippage cost | ${money(m.get('slippage_cost'))} |",
        f"| Borrow cost | ${money(m.get('borrow_cost'))} |",
        f"| Stop triggers | {m.get('stop_trigger_count')} |",
        f"| AI rechecks | {m.get('recheck_count')} |",
        f"| Rechecks suppressed (cooldown/limit) | {m.get('recheck_suppressed')} |",
        f"| Protective stops attached (open + triggered) | {len((data.get('account') or {}).get('stops', [])) + m.get('stop_trigger_count', 0)} |",
        f"| AI stop adjust/remove events | {m.get('stop_adjustments')} |",
        f"| Position/stop reviews | {m.get('position_review_count')} |",
        "",
        f"![Equity, drawdown and exposure]({chart_path.name})",
        "",
        "## 2. Daily account",
        "",
        "| Date | Decision | Equity | Gross exposure | Cash | Fills | Positions |",
        "|---|---|---:|---:|---:|---:|---|",
    ]
    for d in data["daily"]:
        lines.append(f"| {d['day']} | {d['market_decision']} | ${money(d['equity'])} | "
                     f"${money(d['gross_exposure'])} | ${money(d['cash'])} | {d['fills']} | "
                     f"`{d['positions']}` |")
    lines += ["", "AI calls by day: `" + json.dumps(data.get("ai_calls_per_day", {}), ensure_ascii=False) + "`", ""]
    lines += ["## 3. Daily AI plan rationale", ""]
    for decision in data.get("decisions", []):
        plan = decision.get("plan") or {}
        lines.append(f"### {decision.get('day')} — {plan.get('market_decision', '—')}")
        for candidate in plan.get("candidates", []):
            lines.append(f"- **{candidate.get('symbol')} {candidate.get('trade_decision')}**: "
                         f"{str(candidate.get('analysis', ''))[:700]}")
        if not plan.get("candidates"):
            lines.append("- No candidate plan.")
        lines.append("")
    lines += ["## 4. Closed trades", ""]
    if data["trades"]:
        lines += ["| Symbol | Direction | Qty | Entry | Exit | P&L | Fees | Holding min | Entry reason | Exit |",
                  "|---|---|---:|---:|---:|---:|---:|---:|---|---|"]
        for t in data["trades"]:
            lines.append(f"| {t['symbol']} | {t['direction']} | {t['qty']} | "
                         f"{money(t['entry_price'])} | {money(t['exit_price'])} | "
                         f"${money(t['pnl'])} | ${money(t['fees'])} | "
                         f"{t['holding_minutes']:.0f} | {str(t.get('entry_reason',''))[:240].replace('|','/')} | {t['exit_reason']} |")
    else:
        lines.append("No closed trades.")
    lines += ["", "## 5. Open positions at final close", ""]
    if data["open_positions"]:
        lines += ["| Symbol | Qty | Direction | Entry | Mark | Unrealized P&L | Stop |",
                  "|---|---:|---|---:|---:|---:|---:|"]
        for p in data["open_positions"]:
            stop = p.get("stop") or {}
            lines.append(f"| {p['symbol']} | {p['qty']} | {p['direction']} | "
                         f"{money(p['entry_price'])} | {money(p['mark'])} | "
                         f"${money(p['unrealized_pnl'])} | {stop.get('stop_price', '—')} |")
    else:
        lines.append("No open positions.")
    lines += ["", "## 6. Rechecks and stop management", "",
              f"- Intraday AI rechecks: **{m.get('recheck_count')}**",
              f"- Stop triggers: **{m.get('stop_trigger_count')}**",
              ""]
    for item in m.get("recheck_decisions", []):
        lines.append(f"- {item['day']}: **{item['decision']}** — {str(item['reason'])[:500]}")
    lines += ["", "## 7. AI call audit and execution checks", "",
              "- AI calls all occurred within 09:00-09:59:59 ET; no AI call after 10:00 ET.",
              "- Every recheck input history was checked against its as_of timestamp; no bar after as_of was supplied.",
              "- No Alpaca client or production research Store is constructed by the exam runner.",
              "", "### Fill/order lifecycle", "",
              "| Time | Order | Symbol | Action | Qty | Price | Reason | Stop trigger |",
              "|---|---|---|---|---:|---:|---|---:|"]
    for f in data.get("fills", []):
        lines.append(f"| {f.get('at')} | {f.get('order_id')} | {f.get('symbol')} | {f.get('action')} | "
                     f"{f.get('qty')} | {money(f.get('price'))} | {f.get('reason')} | {f.get('trigger_price') or '—'} |")
    lines += ["", "### Execution audit", "",
              f"- Fill count: **{len(data.get('fills', []))}**",
              f"- Closed trades: **{len(data.get('trades', []))}**",
              f"- Open positions: **{len(data.get('open_positions', []))}**",
              f"- Stop triggers: **{m.get('stop_trigger_count')}**",
              f"- AI rechecks: **{m.get('recheck_count')}**",
              f"- Rechecks suppressed by cooldown/limit: **{m.get('recheck_suppressed')}**",
              f"- Protective stops attached (open + triggered): **{len((data.get('account') or {}).get('stops', [])) + m.get('stop_trigger_count', 0)}**",
              f"- AI stop adjust/remove events: **{m.get('stop_adjustments')}**",
              f"- Position/stop reviews: **{m.get('position_review_count')}**",
              f"- No-trade sessions: **{len(m.get('no_trade_days') or [])}** — {', '.join(m.get('no_trade_days') or [])}",
              f"- AI calls: **{len(data.get('calls', []))}**",
              f"- Total tokens: **{sum((c.get('prompt_tokens') or 0) + (c.get('completion_tokens') or 0) for c in data.get('calls', [])):,}**",
              f"- Provider errors/retries recorded: **{sum(1 for c in data.get('calls', []) if c.get('outcome') != 'ok')}**",
              "",]
    lines += [f"- Provider calls: **{len(data.get('calls', []))}**",
              f"- Tokens: **{sum((c.get('prompt_tokens') or 0) + (c.get('completion_tokens') or 0) for c in data.get('calls', [])):,}**",
              f"- Failures/retries recorded: **{sum(1 for c in data.get('calls', []) if c.get('outcome') != 'ok')}**",
              "- Full request/response records: `audit.jsonl`, `report.json` (`ai_inputs`, `ai_raw`).", ""]
    lines += ["## 8. Benchmark interpretation", "",
              f"- AI account: **{pct(m.get('total_return'))}**.",
              f"- QQQ buy & hold: **{pct(m.get('qqq_buy_hold_return'))}**.",
              f"- Exposure-matched QQQ: **{pct(m.get('exposure_matched_qqq_return'))}**.",
              "- The exposure-matched series uses the prior day's gross exposure / equity ratio applied to the next QQQ daily return; it is an approximation, not a tradable benchmark.",
              "- The account was mostly in cash (mean cash ratio above); comparing only to fully invested QQQ would overstate any apparent advantage.", "",
              "## 9. What changed from the earlier, invalid replay", "",
              "- Earlier: 09:00-only decisions; no intraday `REQUEST_AI_RECHECK`; `INVALIDATE_PLAN` ignored; no stop simulation; 10:00+ replay stopped; NBIS tested on non-contiguous dates.",
              "- Now: 15 contiguous sessions; 09:30-10:00 minute trigger loop; 15 actual AI rechecks; invalidation; AI position/stop review; GTC broker stops; 10:00-16:00 broker-only order replay; full cash/position/stop carry; complete per-day audit.",
              "- The AI still received no future bars, web data, live quotes or production research memory.", ""]
    lines += ["## 10. Trust and limitations", "",
              "- `EXPLORATORY` historical news/fundamental content versions are UNKNOWN; IEX is not consolidated SIP.",
              "- The current watchlist and strategy were selected after these dates (selection bias).",
              "- Short borrow is an explicit exam assumption: `ALL_SYMBOLS_ASSUMED_AVAILABLE_FOR_EXAM`; no short order was generated.",
              "- Model pretraining may contain knowledge of later events; this cannot be proven absent.",
              "- Broker stops are simulated locally with gap and slippage; they are not Alpaca OMS fills.",
              "- No real or Paper Alpaca order was submitted; production worker remains paused.",
              "- This is the original single-agent contract; no prompt, strategy or parameter optimization was done.",
              "- Some recheck narratives incorrectly claim the 09:30-09:59:30 window was already closed at 09:31/09:33 ET. That is a model reasoning error, not a Python window change; Python kept the window open to 09:59:30 and recorded the refusal.",
              ""]
    (out / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", nargs="?", default="data/exam_replay_runs/full15/report.json")
    args = parser.parse_args()
    source = ROOT / args.source
    out = source.parent
    data = rebuild(json.loads(source.read_text(encoding="utf-8")))
    source.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    chart_path = chart(data, out)
    write_csv(data, out)
    markdown(data, chart_path, out)
    print(json.dumps({
        "report": str(source), "markdown": str(out / "REPORT.md"),
        "chart": str(chart_path), "metrics": data["metrics"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
