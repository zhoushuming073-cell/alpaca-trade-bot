"""Render a human-readable report and CSVs from one exam replay JSON."""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path


def pct(value):
    return "—" if value is None else f"{float(value) * 100:.3f}%"


def money(value):
    return "—" if value is None else f"{float(value):,.2f}"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source")
    parser.add_argument("--markdown", default="")
    parser.add_argument("--csv-prefix", default="")
    args = parser.parse_args()
    source = Path(args.source)
    report = json.loads(source.read_text(encoding="utf-8"))
    metrics = report.get("metrics") or {}
    markdown = Path(args.markdown) if args.markdown else source.with_suffix(".md")
    lines = [
        f"# Exam replay report — {', '.join(report.get('days', []))}",
        "",
        f"- Dataset mode: `{report.get('exam_mode')}`",
        f"- Model: `{report.get('model')}`",
        f"- Short locate assumption: `{report.get('short_locate_assumption')}`",
        f"- Real orders: **none**",
        f"- Production research memory: **not used**",
        f"- No web search / live quotes: **yes**",
        "",
        "## Performance",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Total return | {pct(metrics.get('total_return'))} |",
        f"| Max drawdown | {pct(metrics.get('max_drawdown'))} |",
        f"| Win rate | {'N/A (no closed trades)' if metrics.get('win_rate') is None else pct(metrics.get('win_rate'))} |",
        f"| Closed trades | {metrics.get('trades', 0)} |",
        f"| QQQ buy & hold | {pct(metrics.get('qqq_buy_hold_return'))} |",
        f"| Capital utilization mean | {pct(metrics.get('capital_utilization_mean'))} |",
        f"| Sharpe (daily, annualized) | {metrics.get('sharpe_252') if metrics.get('sharpe_252') is not None else '—'} |",
        "",
        "## Fills",
        "",
    ]
    fills = report.get("fills") or []
    if fills:
        lines += ["| Time | Symbol | Action | Qty | Price | Fee | Order |",
                  "|---|---|---|---:|---:|---:|---|"]
        for fill in fills:
            lines.append(
                f"| {fill.get('at')} | {fill.get('symbol')} | {fill.get('action')} | "
                f"{fill.get('qty')} | {money(fill.get('price'))} | {money(fill.get('fee'))} | {fill.get('order_id')} |"
            )
    else:
        lines.append("No fills.")
    lines += ["", "## Open positions at final close", ""]
    positions = report.get("open_positions") or []
    if positions:
        lines += ["| Symbol | Qty | Direction | Entry | Mark | Unrealized P&L |",
                  "|---|---:|---|---:|---:|---:|"]
        for item in positions:
            lines.append(
                f"| {item.get('symbol')} | {item.get('qty')} | {item.get('direction')} | "
                f"{money(item.get('entry_price'))} | {money(item.get('mark'))} | {money(item.get('unrealized_pnl'))} |"
            )
    else:
        lines.append("No open positions.")
    lines += ["", "## Daily equity", ""]
    for day in report.get("daily", []):
        lines.append(
            f"- {day.get('day')}: decision `{day.get('market_decision')}`, "
            f"equity {money(day.get('equity'))}, cash {money(day.get('cash'))}, "
            f"positions `{day.get('positions')}`, submitted `{day.get('submitted')}`"
        )
    lines += [
        "", "## Model usage", "",
        f"- Calls: {len(report.get('calls', []))}",
        f"- Tokens: {sum((c.get('prompt_tokens') or 0) + (c.get('completion_tokens') or 0) for c in report.get('calls', []))}",
        "- Cost: UNKNOWN (rates not configured)",
        "", "## Caveats", "",
        "- `EXPLORATORY` includes later-retrieved news/facts; historical content versions are UNKNOWN.",
        "- IEX feed is not consolidated SIP; short locates are explicitly assumed available.",
        "- The current watchlist/strategy were selected after the historical dates (selection bias).",
        "- No real or Paper order was sent; this is an isolated simulator.",
    ]
    markdown.parent.mkdir(parents=True, exist_ok=True)
    markdown.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if args.csv_prefix:
        prefix = Path(args.csv_prefix)
        prefix.parent.mkdir(parents=True, exist_ok=True)
        with prefix.with_name(prefix.name + "_fills.csv").open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(fills[0]) if fills else ["empty"])
            writer.writeheader()
            writer.writerows(fills)
        with prefix.with_name(prefix.name + "_positions.csv").open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(positions[0]) if positions else ["empty"])
            writer.writeheader()
            writer.writerows(positions)
    print(markdown)


if __name__ == "__main__":
    main()
