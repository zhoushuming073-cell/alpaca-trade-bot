"""Historical AI inputs derived only from one V2 snapshot and simulated state."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .snapshot import ExamMode, get_v2_snapshot

ROOT=Path(__file__).resolve().parents[2]
STAGES={"UNIVERSE_SCAN":"universe_scan.txt","PREMARKET_PLAN":"premarket_plan.txt"}
EXAM_GUARD=("HISTORICAL EXAM ONLY. Use only the dated JSON input below. No web search, fetch tools, "
            "live account, production research memory, current news, or later price knowledge. "
            "Treat UNKNOWN material as uncertain. The broker is an isolated simulator; no real orders. "
            "Current strategy rules and the fixed watchlist were selected after these dates and may cause selection bias.\n\n")


def _canon(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":"),default=str)


def _hash(value):return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PromptEnvelope:
    mode: ExamMode
    stage: str
    exam_day: str
    asof_utc: str
    dataset_version: str
    model: str
    system: str
    user: str
    snapshot_sha256: str
    input_sha256: str
    account_snapshot_id: str
    risk_flags: tuple[str,...]

    @property
    def messages(self):
        return [{"role":"system","content":self.system},{"role":"user","content":self.user}]


def _account(state,cutoff):
    if state is None:
        raise ValueError("An explicit historical simulator account snapshot is required")
    if set(state)!={"source","asof_utc","cash","equity","positions","open_orders","historical_short_locates"}:
        raise ValueError("Simulated account state has unsupported fields")
    if state["source"]!="ISOLATED_SIMULATOR":raise ValueError("Historical account must come from isolated simulator")
    stamped=datetime.fromisoformat(str(state["asof_utc"]).replace("Z","+00:00"))
    if stamped.tzinfo is None or stamped!=cutoff:raise ValueError("Simulated account must be marked at exact historical cutoff")
    if not isinstance(state["positions"],dict) or not isinstance(state["open_orders"],list):
        raise ValueError("Invalid simulated positions or orders")
    return state,_hash(_canon(state))[:32]


def _compact(snapshot,symbols):
    out=[]
    for symbol in symbols:
        day=[b for b in snapshot["bars"] if b["symbol"]==symbol and b["timeframe"]=="1Day"][-30:]
        minute=[b for b in snapshot["bars"] if b["symbol"]==symbol and b["timeframe"]=="1Min"][-20:]
        week=[b for b in snapshot["weekly_bars"] if b["symbol"]==symbol][-13:]
        month=[b for b in snapshot["monthly_bars"] if b["symbol"]==symbol][-6:]
        out.append({"symbol":symbol,"data_status":"OK" if day else "UNAVAILABLE",
                    "daily_recent":day,"weekly":week,"monthly":month,"premarket_recent":minute,
                    "identity":snapshot["security_identity"].get(symbol)})
    return out


def build_exam_prompt(day,asof,symbols,*,mode=ExamMode.STRICT,stage="UNIVERSE_SCAN",
                      simulated_account=None,model="deepseek-flash",scan_output_raw=None,
                      db_path=None,_store=None):
    """Construct exact provider messages; perform no AI or external data call."""
    mode=ExamMode(mode)
    if stage not in STAGES:raise ValueError("Unsupported exam stage")
    snapshot=get_v2_snapshot(day,asof,symbols,mode=mode,db_path=db_path,_store=_store)
    cutoff=datetime.fromisoformat(snapshot["asof_utc"]).astimezone(timezone.utc)
    account,account_id=_account(simulated_account,cutoff)
    strategy=yaml.safe_load((ROOT/"config/strategy.yaml").read_text(encoding="utf-8"))
    scan_candidates=[]
    chosen=list(symbols)
    if stage=="PREMARKET_PLAN":
        if scan_output_raw is None:raise ValueError("Premarket plan requires historical scan output")
        from trading_bot.decision_schema import parse_scan
        scan=parse_scan(scan_output_raw,symbols=symbols,now=cutoff)
        if scan.as_of_utc!=cutoff or scan.account_snapshot_id!=account_id or scan.model_id!=model:
            raise ValueError("Scan is not tied to this historical snapshot/account")
        scan_candidates=[c.model_dump(mode="json") for c in scan.candidates]
        chosen=list(dict.fromkeys(c["symbol"] for c in scan_candidates))
    relevant=set(chosen+["QQQ"])
    evidence=[e for e in snapshot["evidence"] if not e.get("symbols") or relevant.intersection(e["symbols"])]
    evidence=evidence[-100:]
    facts=sorted((f for f in snapshot["fundamentals"] if f["symbol"] in relevant),
                 key=lambda f:f["available_at"])[-100:]
    body={"exam":{"version":snapshot["dataset_version"],"mode":mode.value,"stage":stage,
                  "date":snapshot["date"],"as_of_utc":snapshot["asof_utc"],"feed":snapshot["feed"],
                  "no_external_research":True,"current_memory_used":False,
                  "unknown_material_used":mode is ExamMode.EXPLORATORY,
                  "selection_bias":"current watchlist and strategy selected after historical dates"},
          "as_of_utc":snapshot["asof_utc"],"model_id":model,"account_snapshot_id":account_id,
          "strategy":{"version":strategy["version"],"entry_rules":strategy["entry_rules"],
                      "exit_rules":strategy["exit_rules"]},
          "account":account,"universe":_compact(snapshot,chosen),
          "market_reference":{"description":"QQQ is Nasdaq-100 ETF proxy, not Nasdaq Composite; analysis only, not tradable in this exam",
                              "history":_compact(snapshot,["QQQ"])[0]},
          "evidence":evidence,"fundamentals":facts,
          "evidence_omitted_count":max(0,len(snapshot["evidence"])-len(evidence)),
          "facts_omitted_count":max(0,len(snapshot["fundamentals"])-len(facts)),
          "unavailable_symbols":snapshot["unavailable_symbols"],
          "risk_flags":snapshot["limitations"],
          "execution_contract":{"paper_simulation_only":True,"whole_shares":True,"order_type":"limit",
                                "time_in_force":"day","shorting_requires_historical_locate":True,
                                "historical_locate_status":account["historical_short_locates"]}}
    if stage=="PREMARKET_PLAN":
        body["scan_candidates"]=scan_candidates
        body["focus"]=_compact(snapshot,chosen)
    system=EXAM_GUARD+(ROOT/"prompts"/STAGES[stage]).read_text(encoding="utf-8")
    user=_canon(body)
    snapshot_sha=_hash(_canon(snapshot))
    return PromptEnvelope(mode,stage,snapshot["date"],snapshot["asof_utc"],snapshot["dataset_version"],
                          model,system,user,snapshot_sha,_hash(system+"\n"+user),account_id,
                          ("MODEL_PRETRAINING_UNKNOWN","EX_POST_STRATEGY_SELECTION","HISTORICAL_SHORT_LOCATE_UNKNOWN"))


def parse_exam_response(envelope:PromptEnvelope,raw:str,symbols):
    """Reuse production schemas, then bind the output to the historical input."""
    from trading_bot.decision_schema import parse_plan,parse_scan
    cutoff=datetime.fromisoformat(envelope.asof_utc)
    if envelope.stage=="UNIVERSE_SCAN":
        result=parse_scan(raw,symbols=symbols,now=cutoff)
        candidates=result.candidates
    elif envelope.stage=="PREMARKET_PLAN":
        result=parse_plan(raw,symbols=symbols,now=cutoff)
        candidates=result.candidates
    else:raise ValueError("Unknown exam stage")
    if result.as_of_utc!=cutoff or result.model_id!=envelope.model or result.account_snapshot_id!=envelope.account_snapshot_id:
        raise ValueError("AI output does not match its historical input")
    provided=json.loads(envelope.user)
    known={e["id"] for e in provided["evidence"]}
    for candidate in candidates:
        if any(eid not in known for eid in candidate.evidence_ids):
            raise ValueError("AI cited evidence outside this historical input")
    return result
