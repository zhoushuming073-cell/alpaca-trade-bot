import json
import os
import time
from openai import OpenAI
from .settings import ROOT
from .decision_schema import parse

class DeepSeek:
    def __init__(self, key, client=None, audit=None):
        self.model = os.getenv("DEEPSEEK_MODEL", "deepseek-flash")
        self.client = client or OpenAI(api_key=key, base_url="https://api.deepseek.com", timeout=40, max_retries=1)
        self.audit = audit

    def decide(self, payload):
        started = time.monotonic()
        response = self.client.chat.completions.create(
            model=self.model, messages=[
                {"role":"system", "content":(ROOT / "prompts/decision.txt").read_text(encoding="utf-8")},
                {"role":"user", "content":json.dumps(payload, default=str)}],
            response_format={"type":"json_object"}, max_tokens=16000,
            extra_body={"thinking":{"type":"disabled"}})
        choice = response.choices[0]
        if self.audit:
            self.audit("ai_raw",{"cycle":payload.get("cycle"),"model":response.model,"content":choice.message.content,"finish_reason":choice.finish_reason})
        try:
            if choice.finish_reason != "stop":
                raise ValueError("Incomplete model response")
            decisions = parse(choice.message.content, payload["symbols"])
        except ValueError as exc:
            exc.stage="deepseek_schema"
            raise
        return decisions, {"model":self.model, "resolved_model":response.model, "latency_seconds":time.monotonic()-started}
