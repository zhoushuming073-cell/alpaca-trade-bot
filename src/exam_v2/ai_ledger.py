"""Separate immutable-by-call AI transcript ledgers; no provider is configured here."""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .prompts import PromptEnvelope
from .snapshot import ExamMode

ROOT=Path(__file__).resolve().parents[2]


class ExamCallLedger:
    def __init__(self,mode,*,directory=None):
        self.mode=ExamMode(mode)
        directory=Path(directory or ROOT/"data/exam_v2_runs")
        directory.mkdir(parents=True,exist_ok=True)
        self.path=directory/(self.mode.value.lower()+".sqlite")
        self.db=sqlite3.connect(self.path)
        self.db.execute("""CREATE TABLE IF NOT EXISTS calls(
          call_id TEXT PRIMARY KEY, dataset_version TEXT NOT NULL, exam_day TEXT NOT NULL,
          mode TEXT NOT NULL, stage TEXT NOT NULL, asof_utc TEXT NOT NULL,
          input_sha256 TEXT NOT NULL, snapshot_sha256 TEXT NOT NULL,
          system_prompt TEXT NOT NULL, user_prompt TEXT NOT NULL, model_config_json TEXT NOT NULL,
          invoked_at_utc TEXT NOT NULL, completed_at_utc TEXT,
          raw_output TEXT, parsed_output_json TEXT, usage_json TEXT,
          status TEXT NOT NULL, error_type TEXT)""")
        self.db.commit()

    def close(self):self.db.close()

    def __enter__(self):return self

    def __exit__(self,*_):self.close()

    def start(self,envelope:PromptEnvelope,model_config:dict):
        if envelope.mode is not self.mode:raise ValueError("Mode ledger mismatch")
        call_id=str(uuid.uuid4())
        now=datetime.now(timezone.utc).isoformat()
        self.db.execute("INSERT INTO calls VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [call_id,envelope.dataset_version,envelope.exam_day,self.mode.value,envelope.stage,
             envelope.asof_utc,envelope.input_sha256,envelope.snapshot_sha256,
             envelope.system,envelope.user,json.dumps(model_config,sort_keys=True),now,
             None,None,None,None,"SENT",None])
        self.db.commit()
        return call_id

    def finish(self,call_id,raw_output,*,parsed_output=None,usage=None,error_type=None):
        row=self.db.execute("SELECT status FROM calls WHERE call_id=?",[call_id]).fetchone()
        if row is None or row[0]!="SENT":raise ValueError("Unknown or already completed call")
        self.db.execute("""UPDATE calls SET completed_at_utc=?,raw_output=?,parsed_output_json=?,
            usage_json=?,status=?,error_type=? WHERE call_id=?""",
            [datetime.now(timezone.utc).isoformat(),raw_output,
             json.dumps(parsed_output,ensure_ascii=False,default=str) if parsed_output is not None else None,
             json.dumps(usage,ensure_ascii=False) if usage is not None else None,
             "ERROR" if error_type else "DONE",error_type,call_id])
        self.db.commit()


def call_historical_ai(envelope:PromptEnvelope,transport:Callable,parser:Callable,*,authorized=False,
                       model_config=None,ledger_directory=None):
    """Future entrypoint: explicit authorization and injected tool-free transport required.

    This function is never used by the V2 builder or audit. The transport is
    supplied by a future approved run; no DeepSeek key or network client exists
    in the exam package itself.
    """
    if not authorized:raise PermissionError("Historical AI exam has not been authorized")
    if not callable(transport):raise TypeError("An explicit tool-free transport is required")
    if not callable(parser):raise TypeError("A production-schema parser is required")
    allowed={"model","temperature","top_p","max_tokens","response_format","seed"}
    if model_config is not None and set(model_config)-allowed:
        raise ValueError("Unsupported model configuration field; credentials and tools are forbidden")
    settings={"model":envelope.model,"temperature":0.2,"response_format":"json_object",
              **(model_config or {})}
    if settings["model"]!=envelope.model:
        raise ValueError("Model configuration does not match the recorded prompt")
    with ExamCallLedger(envelope.mode,directory=ledger_directory) as ledger:
        call_id=ledger.start(envelope,settings)
        output=None
        try:
            output=transport(envelope.messages,settings)
            if not isinstance(output,str):raise TypeError("Transport must return raw text")
            parsed=parser(output)
            structured=parsed.model_dump(mode="json") if hasattr(parsed,"model_dump") else parsed
            ledger.finish(call_id,output,parsed_output=structured)
            return call_id,parsed
        except Exception as exc:
            ledger.finish(call_id,output,error_type=type(exc).__name__)
            raise
