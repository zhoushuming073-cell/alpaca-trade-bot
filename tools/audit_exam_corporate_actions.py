"""Read-only Alpaca corporate-action check for the frozen exam universe."""
from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))


def main():
    from trading_bot.dashboard import alpaca_environment
    credentials=alpaca_environment()
    symbols=yaml.safe_load((ROOT/"config/watchlist.yaml").read_text(encoding="utf-8"))["symbols"]+["QQQ"]
    http=requests.Session()
    http.headers.update({"APCA-API-KEY-ID":credentials["ALPACA_API_KEY"],
                         "APCA-API-SECRET-KEY":credentials["ALPACA_API_SECRET"]})
    endpoint="https://data.alpaca.markets/v1/corporate-actions"
    params={"symbols":",".join(symbols),"start":"2026-02-13","end":"2026-11-05",
            "limit":1000,"data_quality":"all","region":"all","sort":"asc"}
    records=[];pages=0
    while pages<10:
        response=http.get(endpoint,params=params,timeout=(5,30))
        response.raise_for_status()
        payload=response.json();pages+=1
        for kind,items in (payload.get("corporate_actions") or {}).items():
            if isinstance(items,list):
                for item in items:records.append({"type":kind,"data":item})
        token=payload.get("next_page_token")
        if not token:break
        params["page_token"]=token
    else:raise RuntimeError("Corporate actions pagination did not finish")
    output={"source":endpoint,"retrieved_at":datetime.now(timezone.utc).isoformat(),
            "start":params["start"],"end":params["end"],"data_quality":"all",
            "pages":pages,"count":len(records),"types":dict(Counter(r["type"] for r in records)),
            "records":records,
            "historical_limit":"Alpaca does not guarantee original creation/announcement time of these records"}
    path=ROOT/"data/lookahead_audit/corporate_actions.json"
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(output,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({k:v for k,v in output.items() if k!="records"},ensure_ascii=False))


if __name__=="__main__":main()
