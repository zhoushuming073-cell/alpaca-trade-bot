"""Frozen 15-session v3 historical exam runner and report.

No Alpaca import, no broker client, no web tools. The only network client is the
explicit DeepSeek JSON transport. The simulator is isolated and no order is sent
to Alpaca.
"""
from __future__ import annotations

import argparse, csv, hashlib, json, os, statistics, sys, time
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from exam_data import ExamStore  # noqa: E402
from exam_data.store import ET  # noqa: E402
from exam_v3.engine import Engine  # noqa: E402
from exam_v3.evaluation import qqq_buy_hold  # noqa: E402

DATES = ["2026-08-17", "2026-08-18", "2026-08-19", "2026-08-20", "2026-08-21",
         "2026-08-24", "2026-08-25", "2026-08-26", "2026-08-27", "2026-08-28",
         "2026-08-31", "2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"]
OUT = ROOT / "data/exam_v3_run_20260817_0904_r4"
DB = ROOT / "data/exam_v2.duckdb"
MODEL = "deepseek-flash"


def sha(path):
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def sources():
    paths = list((ROOT / "src/exam_v3").glob("*.py")) + [
        ROOT / "prompts/decision_v3.txt", ROOT / "prompts/scan_v3.txt", ROOT / "config/aggressive_paper_v3.yaml",
        Path(__file__), ROOT / "src/exam_v2/snapshot.py", ROOT / "src/exam_data/store.py",
        ROOT / "requirements.lock.txt", ROOT / "pyproject.toml",
    ]
    return {p.relative_to(ROOT).as_posix(): sha(p) for p in paths}


def freeze():
    if OUT.exists():
        raise SystemExit("refusing to overwrite existing exam output")
    OUT.mkdir(parents=True)
    payload = {
        "version": "decision-v3-full-exam-20260817-0904-r4",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "dates": DATES, "mode": "EXPLORATORY", "model": MODEL,
        "temperature": 0.2, "max_tokens": 8000, "max_calls_per_day": 8,
        "initial_cash": 100000, "initial_positions": "FLAT",
        "symbols": __import__("yaml").safe_load((ROOT / "config/watchlist.yaml").read_text(encoding="utf-8"))["symbols"],
        "short_locate_assumption": "ALL_SYMBOLS_ASSUMED_AVAILABLE_FOR_EXAM",
        "fees_per_share": "0.005", "slippage_bps": 5, "participation": "0.10",
        "annual_borrow_rate": "0.08",
        "sources": sources(), "dataset_sha256": sha(DB),
        "no_web": True, "no_production_memory": True, "no_alpaca": True,
    }
    payload["combined_sha256"] = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    (OUT / "freeze.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def verify(frozen):
    current = {p: sha(ROOT / p) for p in frozen["sources"]}
    if current != frozen["sources"] or sha(DB) != frozen["dataset_sha256"]:
        raise SystemExit("frozen source/data hash mismatch; refusing exam")
    if [str(x) for x in DATES] != frozen["dates"]:
        raise SystemExit("date list mismatch")


def transport_factory(audit_path, token_limit):
    from openai import OpenAI
    key = os.environ.get("DEEPSEEK_API_KEY") or (ROOT.parent / "api-key.txt").read_text(encoding="utf-8-sig").strip()
    client = OpenAI(api_key=key, base_url="https://api.deepseek.com", timeout=120, max_retries=0)
    total = {"tokens": 0}
    handle = audit_path.open("w", encoding="utf-8")
    def call(messages, config):
        started = time.monotonic()
        response = client.chat.completions.create(
            model=config["model"], messages=messages, temperature=config["temperature"],
            max_tokens=config["max_tokens"], response_format={"type": "json_object"},
            extra_body={"thinking": {"type": "disabled"}},
        )
        choice = response.choices[0]; usage = response.usage
        if choice.finish_reason != "stop":
            raise ValueError("incomplete model response: " + str(choice.finish_reason))
        used = int(usage.prompt_tokens or 0) + int(usage.completion_tokens or 0)
        total["tokens"] += used
        if total["tokens"] > token_limit:
            raise RuntimeError("token budget exceeded")
        handle.write(json.dumps({"at_utc": datetime.now(timezone.utc).isoformat(),
                                 "model": response.model, "provider_id": response.id,
                                 "usage": usage.model_dump(), "latency_ms": int((time.monotonic()-started)*1000),
                                 "messages": messages, "raw": choice.message.content}, ensure_ascii=False) + "\n")
        handle.flush()
        return choice.message.content or ""
    return call, handle, total


def trade_ledger(fills):
    lots = {}; trades = []
    for f in fills:
        s = f["symbol"]; qty = int(f["qty"]); price = float(f["price"]); delta = int(f["delta"])
        fee = float(f["fee"]); at = f["at"]
        if delta > 0:
            lots.setdefault(s, []).append({"qty": qty, "price": price, "fee": fee, "at": at, "direction": "long"})
            continue
        remaining = qty
        while remaining and lots.get(s):
            lot = lots[s][0]
            q = min(remaining, lot["qty"])
            direction = lot["direction"]
            pnl = (price - lot["price"]) * q if direction == "long" else (lot["price"] - price) * q
            entry_fee = lot["fee"] * q / lot["qty"]
            close_fee = fee * q / qty
            trades.append({"symbol": s, "direction": direction, "qty": q,
                           "entry_price": lot["price"], "exit_price": price,
                           "entry_at": lot["at"], "exit_at": at,
                           "pnl": pnl - entry_fee - close_fee, "fees": entry_fee + close_fee,
                           "holding_minutes": (datetime.fromisoformat(at) - datetime.fromisoformat(lot["at"])).total_seconds()/60,
                           "entry_order": f.get("order_id"), "exit_order": f.get("order_id")})
            lot["qty"] -= q; lot["fee"] -= entry_fee; remaining -= q
            if lot["qty"] <= 0: lots[s].pop(0)
    return trades


def qqq_price_at(conn, ts):
    row = conn.execute("SELECT close FROM bars WHERE symbol='QQQ' AND timeframe='1Min' AND feed='iex' AND ts<=? ORDER BY ts DESC LIMIT 1", [ts]).fetchone()
    return float(row[0]) if row else None


def report(frozen):
    records = [json.loads(x) for x in (OUT / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    daily = [r for r in records if r["kind"] == "daily_account"]
    inputs = [r for r in records if r["kind"] == "input"]
    outputs = [r for r in records if r["kind"] == "output"]
    rejected = [r for r in records if r["kind"] == "rejected_output"]
    snapshots = [r for r in records if r["kind"] == "snapshot"]
    fills = [f for f in (daily[-1].get("fills", []) if daily else [])]
    equity = [float(r["account"]["equity"]) for r in daily]
    peak = equity[0]; maxdd = 0.0
    for value in equity:
        peak = max(peak, value); maxdd = max(maxdd, 1-value/peak)
    trades = trade_ledger(fills)
    wins = [t for t in trades if t["pnl"] > 0]
    import duckdb
    conn = duckdb.connect(str(DB), read_only=True)
    qqq = qqq_buy_hold(conn, [date.fromisoformat(d) for d in DATES])
    qqq_points = []
    for snap in snapshots:
        at = datetime.fromisoformat(snap["asof"])
        p = qqq_price_at(conn, at)
        if p is not None: qqq_points.append((at, p))
    # Exposure-matched uses the observed gross exposure at each review snapshot.
    exp_value = 1.0
    ordered_snaps = sorted(snapshots, key=lambda x: x["asof"])
    for i in range(1, len(ordered_snaps)):
        t0 = datetime.fromisoformat(ordered_snaps[i-1]["asof"])
        t1 = datetime.fromisoformat(ordered_snaps[i]["asof"])
        p0 = qqq_price_at(conn, t0); p1 = qqq_price_at(conn, t1)
        if not p0 or not p1: continue
        e = float(ordered_snaps[i-1]["account"]["equity"]); g = float(ordered_snaps[i-1]["account"]["gross_exposure"])
        if e: exp_value *= 1 + (g/e)*(p1/p0-1)
    conn.close()
    gross = [float(r["account"]["gross_exposure"]) for r in daily]
    cash = [float(r["account"]["cash"]) for r in daily]
    metrics = {"total_return": equity[-1]/equity[0]-1, "max_drawdown": maxdd,
               "closed_trades": len(trades), "win_rate": len(wins)/len(trades) if trades else None,
               "qqq_buy_hold_return": qqq, "exposure_matched_return": exp_value-1,
               "capital_utilization_mean": statistics.mean(g/e for g,e in zip(gross,equity)) if equity else None,
               "cash_ratio_mean": statistics.mean(c/e for c,e in zip(cash,equity)) if equity else None,
               "ai_calls": len(inputs), "ai_outputs": len(outputs), "rejected_outputs": len(rejected),
               "fills": len(fills), "open_positions": list(daily[-1]["account"]["positions"].items()) if daily else [],
               "fees": float(sum(float(f["fee"]) for f in fills)),
               "realized_pnl": float(sum(t["pnl"] for t in trades)),
               "unrealized": daily[-1]["account"]["equity"] - daily[-1]["account"]["cash"] if daily else None}
    result = {"frozen": frozen, "dates": DATES, "daily": daily, "inputs": inputs,
              "outputs": outputs, "rejected_outputs": rejected, "trades": trades,
              "metrics": metrics, "no_alpaca_order": True}
    (OUT / "report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    lines = ["# v3 15-session exam — " + DATES[0] + " to " + DATES[-1], "",
             f"- Return: **{metrics['total_return']*100:.3f}%**",
             f"- Max drawdown: **{metrics['max_drawdown']*100:.3f}%**",
             f"- Closed trades: **{metrics['closed_trades']}**; win rate: **{metrics['win_rate'] if metrics['win_rate'] is not None else 'N/A'}**",
             f"- QQQ: **{qqq*100:.3f}%**; exposure-matched: **{metrics['exposure_matched_return']*100:.3f}%**",
             f"- AI calls: **{metrics['ai_calls']}**; fills: **{metrics['fills']}**",
             f"- Mean utilization: **{metrics['capital_utilization_mean']*100:.3f}%**; cash: **{metrics['cash_ratio_mean']*100:.3f}%**",
             f"- Frozen hash: `{frozen['combined_sha256']}`",
             "", "## Daily equity", "", "| Day | Equity | Cash | Gross |", "|---|---:|---:|---:|"]
    for r in daily:
        lines.append(f"| {r['day']} | {r['account']['equity']:.2f} | {r['account']['cash']:.2f} | {r['account']['gross_exposure']:.2f} |")
    lines += ["", "## Trades", ""]
    if trades:
        lines += ["| Symbol | Direction | Qty | Entry | Exit | P&L |", "|---|---|---:|---:|---:|---:|"]
        for t in trades:
            lines.append(f"| {t['symbol']} | {t['direction']} | {t['qty']} | {t['entry_price']:.2f} | {t['exit_price']:.2f} | {t['pnl']:.2f} |")
    else:
        lines.append("No closed trades.")
    (OUT / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return metrics


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--freeze-only", action="store_true")
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args()
    if args.freeze_only:
        print(json.dumps(freeze(), ensure_ascii=False)); return
    frozen = json.loads((OUT / "freeze.json").read_text(encoding="utf-8"))
    verify(frozen)
    if not args.run: raise SystemExit("use --run after freeze")
    transport, handle, total = transport_factory(OUT / "transport.jsonl", 6_000_000)
    import yaml
    syms = yaml.safe_load((ROOT / "config/watchlist.yaml").read_text(encoding="utf-8"))["symbols"]
    locates = {(date.fromisoformat(d), s): True for d in DATES for s in syms}
    engine = Engine(syms, DB, transport=transport, output=OUT / "audit.jsonl",
                    model=MODEL, mode="EXPLORATORY", short_locates=locates,
                    risk_profile={"enabled": False})
    try:
        for day in DATES: engine.run_day(day)
    finally:
        engine.close(); handle.close()
    metrics = report(frozen)
    print(json.dumps(metrics, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
