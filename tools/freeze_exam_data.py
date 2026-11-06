"""Audit and freeze the isolated exam database; never touches the trading DB."""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, time, timezone
from pathlib import Path

import yaml

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from exam_data.store import ExamStore, ET, get_exam_snapshot, sessions  # noqa:E402

DB=ROOT/"data/exam_data.duckdb"
VERSION="exam-2026-11-05-v1"


def scalar(store,query,args=()):
    return store.db.execute(query,args).fetchone()[0]


def main():
    symbols=yaml.safe_load((ROOT/"config/watchlist.yaml").read_text(encoding="utf-8"))["symbols"]
    days=sessions(datetime(2026,11,6,12,tzinfo=timezone.utc))
    with ExamStore(DB) as store:
        if store.db.execute("SELECT value FROM meta WHERE key='freeze_version'").fetchone():
            raise RuntimeError("Dataset is already frozen")
        minute_done=scalar(store,"SELECT COUNT(*) FROM jobs WHERE kind='minute' AND job_key LIKE 'minute:sip:%' AND status='done'")
        news_done=scalar(store,"SELECT COUNT(*) FROM jobs WHERE kind='news' AND status='done'")
        market_news_done=scalar(store,"SELECT COUNT(*) FROM jobs WHERE kind='market-news' AND status='done'")
        sec_done=scalar(store,"SELECT COUNT(*) FROM jobs WHERE kind='sec' AND status='done'")
        coverage_count=scalar(store,"SELECT COUNT(*) FROM coverage")
        if minute_done!=60 or news_done!=60 or market_news_done!=60 or coverage_count!=60*49:
            raise RuntimeError(f"Incomplete dataset: minute={minute_done}, news={news_done}, market_news={market_news_done}, coverage={coverage_count}")
        issues=store.db.execute("SELECT job_key,error FROM jobs WHERE status='failed'").fetchall()
        if issues:raise RuntimeError(f"Unresolved failed jobs: {[i[0] for i in issues]}")
        bad_ohlc=scalar(store,"""SELECT COUNT(*) FROM bars WHERE feed='sip' AND
            (open<=0 OR high<greatest(open,close) OR low>least(open,close) OR volume<0)""")
        bad_minute_time=scalar(store,"""SELECT COUNT(*) FROM bars WHERE feed='sip' AND timeframe='1Min'
            AND (end_at!=ts+INTERVAL 1 MINUTE OR available_at<end_at)""")
        if bad_ohlc or bad_minute_time:raise RuntimeError("Invalid source bars or completion timestamps")
        per_symbol=store.db.execute("""SELECT symbol,COUNT(*) AS observed_days,
            SUM(regular_rows) AS regular_count, SUM(missing_regular) AS missing_count,
            COUNT(*) FILTER(WHERE premarket_rows>0) AS premarket_days,
            SUM(premarket_rows) AS premarket_count FROM coverage GROUP BY symbol ORDER BY symbol""").fetchall()
        per_day=store.db.execute("""SELECT CAST(day AS VARCHAR),SUM(regular_rows),SUM(missing_regular),
            SUM(premarket_rows) FROM coverage GROUP BY day ORDER BY day""").fetchall()
        market=store.db.execute("SELECT value FROM meta WHERE key='index_reference'").fetchone()[0]
        sample_day=days[len(days)//2][0]
        sample=get_exam_snapshot(sample_day,datetime.combine(sample_day,time(9),ET),symbols,_store=store)
        cutoff=datetime.fromisoformat(sample["asof"])
        if any(b["available_at"]>cutoff for b in sample["bars"]):raise RuntimeError("Future price leak")
        if any(e["first_published"]>cutoff or e["updated_at"]>cutoff for e in sample["evidence"]):
            raise RuntimeError("Future evidence leak")
        if any(f["available_at"]>cutoff for f in sample["fundamentals"]):raise RuntimeError("Future fundamental leak")
        sample_path=ROOT/"data"/f"exam_snapshot_{sample_day}_0900.json"
        sample_path.write_text(json.dumps(sample,ensure_ascii=False,default=str,indent=2),encoding="utf-8")
        counts={
            "daily_bars":scalar(store,"SELECT COUNT(*) FROM bars WHERE feed='sip' AND timeframe='1Day'"),
            "minute_bars":scalar(store,"SELECT COUNT(*) FROM bars WHERE feed='sip' AND timeframe='1Min'"),
            "regular_minutes":sum(int(row[2]) for row in per_symbol),
            "missing_regular_minutes":sum(int(row[3]) for row in per_symbol),
            "premarket_minutes":sum(int(row[5]) for row in per_symbol),
            "alpaca_news":scalar(store,"SELECT COUNT(*) FROM evidence WHERE kind='news'"),
            "sec_filings":scalar(store,"SELECT COUNT(*) FROM evidence WHERE kind='sec_filing'"),
            "sec_facts":scalar(store,"SELECT COUNT(*) FROM fundamentals"),
            "symbols_with_sec_facts":scalar(store,"SELECT COUNT(DISTINCT symbol) FROM fundamentals"),
        }
        metadata={"version":VERSION,"exam_start":str(days[0][0]),"exam_end":str(days[-1][0]),
                  "trade_symbols":len(symbols),"market_reference":market,"feed":"sip","adjustment":"raw",
                  "minute_done_days":minute_done,"news_done_days":news_done,
                  "market_news_done_days":market_news_done,"sec_done_symbols":sec_done,
                  "counts":counts,"bad_ohlc":bad_ohlc,"bad_minute_time":bad_minute_time,
                  "sample_day":str(sample_day),"sample_asof":sample["asof"],
                  "sample_visible_bars":len(sample["bars"]),"sample_visible_evidence":len(sample["evidence"]),
                  "sample_visible_fundamentals":len(sample["fundamentals"]),
                  "sample_file":sample_path.relative_to(ROOT).as_posix()}
        store.meta("freeze_version",VERSION)
        store.meta("freeze_at",datetime.now(timezone.utc).isoformat())
        store.meta("freeze_metadata",json.dumps(metadata,sort_keys=True))
        store.db.execute("CHECKPOINT")
    with DB.open("rb") as handle:
        digest=hashlib.file_digest(handle,"sha256").hexdigest()
    metadata["db_sha256"]=digest
    metadata["db_bytes"]=DB.stat().st_size
    manifest=ROOT/"data/exam_data.manifest.json"
    manifest.write_text(json.dumps(metadata,ensure_ascii=False,indent=2),encoding="utf-8")
    report=ROOT/"EXAM_DATA_QUALITY.md"
    lines=["# 60 日历史考试数据质量报告","",f"版本：`{VERSION}`。日期：{days[0][0]}—{days[-1][0]}，按 NYSE 日历和美东时间选取已结束的 60 个交易日。",
           "",f"数据库：`data/exam_data.duckdb`（{metadata['db_bytes']:,} 字节；SHA-256 `{digest}`）。",
           f"市场参考：`QQQ`，纳斯达克 100 ETF 代理；**不是**纳斯达克综合指数 `^IXIC`，也不属于 48 只可交易股票。",
           "","## 数据范围与缺口","",
           f"- SIP 原始未复权日 K 线：{counts['daily_bars']:,} 条，覆盖考试日之前约六个月回看；由可见日线计算周线和月线。拆股和分红可能造成价格跳变。",
           f"- 04:00—16:00 ET 起始时刻的一分钟 K 线：{counts['minute_bars']:,} 条，60/60 日抓取完成；16:00 起始的一分钟属于盘后，不计入正常时段覆盖率。正常交易时段 {counts['regular_minutes']:,} 条；缺 {counts['missing_regular_minutes']:,} 条（占预期 49×60×390 条的 {counts['missing_regular_minutes']/(49*60*390):.2%}）。无成交或源数据缺失均可能导致空分钟，未插值。",
           f"- 盘前一分钟 K 线：{counts['premarket_minutes']:,} 条；每个股票日期的实际覆盖见下表。",
           f"- Alpaca 历史新闻：{counts['alpaca_news']:,} 条；60/60 日采集完成。历史原稿版本没有独立存档，严格原文版本可验证比例为 0%。",
           f"- SEC EDGAR 公告：{counts['sec_filings']:,} 条；财务事实：{counts['sec_facts']:,} 条，覆盖 {counts['symbols_with_sec_facts']}/48 只股票。TSM、SKHY 无所选 XBRL 事实。以 SEC 接收时间决定可见性；现时 API 返回的内容可能包含后续修订。",
           "- 行业和宏观事件没有取得可靠的逐条历史版本，本版没有填造；市场新闻以与股票关联的 Alpaca 新闻为主，不能视为完整宏观新闻库。",
           "","## 逐股一分钟覆盖","","| 股票 | 交易时段分钟 | 缺失分钟 | 有盘前数据天数 | 盘前分钟 |","|---|---:|---:|---:|---:|"]
    lines += [f"| {s} | {r:,} | {m:,} | {p}/60 | {n:,} |" for s,_,r,m,p,n in per_symbol]
    lines += ["","## 逐日缺失分钟","","| 日期 | 交易时段分钟 | 缺失分钟 | 盘前分钟 |","|---|---:|---:|---:|"]
    lines += [f"| {d} | {r:,} | {m:,} | {p:,} |" for d,r,m,p in per_day]
    lines += ["","## 历史隔离与可重复性","",
              "`get_exam_snapshot(date, asof_time, symbols)` 只返回该美东交易日、指定时刻已经结束并过了可用缓冲时间的分钟 K 线，以及此前可见的日、周、月 K 线。新闻必须同时满足首次发布时间与更新时间不晚于该时刻；SEC 事实按公告接收时间过滤。",
              f"真实 09:00 ET 数据包：`{metadata['sample_file']}`；其中 {metadata['sample_visible_bars']:,} 条 K 线、{metadata['sample_visible_evidence']} 条新闻/公告、{metadata['sample_visible_fundamentals']} 条财务事实。",
              "行情是事后从 Alpaca SIP 获取，可能包含供应商更正；新闻当前版本无法证明与当时原文逐字一致；SEC 公告接收时间可核验，但现时财务事实亦可能含后续修订。因此价格与 SEC 时间适合严格时间过滤，内容版本只能作为探索性历史重建。",
              "数据库含来源、抓取时间、内容哈希、状态与作业记录；来源和时间戳空值、OHLC 异常及分钟完成时间异常检查均为 0。本版 SHA-256 记录在 `data/exam_data.manifest.json`。采集脚本遇到冻结标记会拒绝写入。",
              "","## 调用","","```python","from exam_data import get_exam_snapshot","snapshot = get_exam_snapshot('2026-09-25', '2026-09-25T09:00:00-04:00', ['AMD'])","```",
              "传入 `symbols` 只能是固定交易股票池；`QQQ` 市场参考自动附加，不应作为交易标的传入。此数据库不含回测、AI 历史交易决策或订单。",
              "","## 来源","","- [Alpaca 历史股票行情](https://docs.alpaca.markets/us/reference/stockbars) 与 [历史新闻](https://docs.alpaca.markets/us/docs/historical-news-data)。","- [SEC EDGAR 数据 API](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)。",""]
    report.write_text("\n".join(lines),encoding="utf-8")
    print(json.dumps(metadata,ensure_ascii=False))


if __name__=="__main__":main()
