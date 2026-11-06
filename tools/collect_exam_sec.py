"""Collect free SEC EDGAR filing metadata and selected XBRL facts, GET-only."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from exam_data.store import ExamStore, ET, digest  # noqa:E402

SEC="https://www.sec.gov"
DATA="https://data.sec.gov"
FORMS={"10-K","10-Q","8-K","20-F","6-K","40-F"}
TAGS={"Revenues","RevenueFromContractWithCustomerExcludingAssessedTax","NetIncomeLoss",
      "Assets","StockholdersEquity","EarningsPerShareDiluted","OperatingIncomeLoss",
      "CashAndCashEquivalentsAtCarryingValue","LongTermDebt","CommonStockSharesOutstanding"}


class SecClient:
    def __init__(self, contact, request_budget):
        self.http=requests.Session()
        self.http.headers.update({"User-Agent":f"AlpacaHistoricalExam/1.0 {contact}","Accept-Encoding":"gzip, deflate"})
        self.calls=0
        self.budget=request_budget

    def get(self,url):
        for attempt in range(3):
            if self.calls>=self.budget:raise RuntimeError("SEC request budget exhausted")
            self.calls+=1
            try:
                response=self.http.get(url,timeout=(5,35))
                if response.status_code in {429,500,502,503,504} and attempt<2:
                    time.sleep(2**attempt+2);continue
                response.raise_for_status()
                time.sleep(0.2)  # SEC asks for at most 10 requests/second.
                return response.json()
            except (requests.Timeout,requests.ConnectionError):
                if attempt==2:raise
                time.sleep(2**attempt+1)
        raise RuntimeError("SEC GET retry exhausted")


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--db",default=str(ROOT/"data/exam_data.duckdb"))
    parser.add_argument("--max-requests",type=int,default=110)
    args=parser.parse_args()
    # SEC requires an identifiable contact. Use the repository's already configured
    # email solely in the SEC User-Agent; do not print or persist it in the exam DB.
    contact=subprocess.run(["git","config","user.email"],cwd=ROOT,capture_output=True,text=True,check=True).stdout.strip()
    if "@" not in contact:raise ValueError("Configure git user.email for the SEC User-Agent")
    client=SecClient(contact,args.max_requests)
    symbols=yaml.safe_load((ROOT/"config/watchlist.yaml").read_text(encoding="utf-8"))["symbols"]
    with ExamStore(args.db) as store:
        if store.db.execute("SELECT value FROM meta WHERE key='freeze_version'").fetchone():
            raise RuntimeError("Exam database is frozen; create a new version before collecting more data")
        start=date.fromisoformat(store.db.execute("SELECT value FROM meta WHERE key='exam_start'").fetchone()[0])-timedelta(days=190)
        end=date.fromisoformat(store.db.execute("SELECT value FROM meta WHERE key='exam_end'").fetchone()[0])
        mapping=client.get(SEC+"/files/company_tickers.json")
        ciks={item["ticker"].upper():int(item["cik_str"]) for item in mapping.values()}
        failures=[]
        for symbol in symbols:
            cik=ciks.get(symbol)
            if cik is None:
                store.job("sec:"+symbol,"sec",None,"unmapped",error="Ticker absent from current SEC mapping")
                failures.append([symbol,"unmapped"])
                continue
            if store.job_done("sec:"+symbol):continue
            count=0
            try:
                submissions=client.get(DATA+f"/submissions/CIK{cik:010d}.json")
                recent=submissions.get("filings",{}).get("recent",{})
                accession_to_time={}
                for form,accession,document,acceptance in zip(
                        recent.get("form",[]),recent.get("accessionNumber",[]),
                        recent.get("primaryDocument",[]),recent.get("acceptanceDateTime",[])):
                    try:
                        # SEC timestamps are Eastern local time; use the actual DST offset.
                        accepted=datetime.fromisoformat(acceptance).replace(tzinfo=ET).astimezone(timezone.utc)
                    except (ValueError,TypeError):continue
                    accession_to_time[accession]=accepted
                    if form in FORMS and start<=accepted.astimezone(ET).date()<=end and document:
                        store.put_sec_filing(symbol,cik,form,accession,document,accepted)
                        count+=1
                companyfacts=client.get(DATA+f"/api/xbrl/companyfacts/CIK{cik:010d}.json")
                rows=[]
                for taxonomy,tags in companyfacts.get("facts",{}).items():
                    for tag,definition in tags.items():
                        if tag not in TAGS:continue
                        for unit,entries in definition.get("units",{}).items():
                            for item in entries:
                                accession=item.get("accn")
                                accepted=accession_to_time.get(accession)
                                if accepted is None or not start<=accepted.astimezone(ET).date()<=end:continue
                                try:
                                    period_end=date.fromisoformat(item["end"])
                                    # Instant facts have no start; use their end date in
                                    # both fields so the composite key remains non-null.
                                    period_start=date.fromisoformat(item["start"]) if item.get("start") else period_end
                                    value=float(item["val"])
                                except (KeyError,TypeError,ValueError):continue
                                source_url=f"{SEC}/Archives/edgar/data/{cik}/{accession.replace('-','')}/"
                                payload={"symbol":symbol,"taxonomy":taxonomy,"tag":tag,"unit":unit,
                                         "start":str(period_start),"end":str(period_end),"accession":accession,"value":value}
                                rows.append([symbol,taxonomy,tag,unit,period_start,period_end,item.get("form") or "",
                                             accession,accepted,value,source_url,datetime.now(timezone.utc),
                                             "sec_acceptance_timestamp_verified_content_reconstructed",digest(payload)])
                store.put_fundamentals(rows)
                store.job("sec:"+symbol,"sec",None,"done",count)
                print(json.dumps({"symbol":symbol,"filings":count,"facts":len(rows),"requests":client.calls}),flush=True)
            except Exception as exc:
                store.job("sec:"+symbol,"sec",None,"failed",count,type(exc).__name__+": "+str(exc))
                failures.append([symbol,type(exc).__name__])
                print(json.dumps({"symbol":symbol,"error":type(exc).__name__}),flush=True)
        print(json.dumps({"requests":client.calls,"failures":failures,"filings":store.scalar("SELECT COUNT(*) FROM evidence WHERE kind='sec_filing'"),
                          "facts":store.scalar("SELECT COUNT(*) FROM fundamentals")}),flush=True)


if __name__=="__main__":main()
