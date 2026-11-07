"""DeepSeek JSON client with bounded parsing repair and auditable usage.

The client never has tools and never sees Alpaca credentials.  It receives only
the strategy/data payload, returns validated Pydantic objects, and records the
model, latency, token usage, finish reason and call outcome.

Invalid JSON gets at most one repair request, and the repair cannot authorize a
trade by itself: the repaired object still passes the same strict schema and the
same Python risk/session checks.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from openai import OpenAI

from .settings import ROOT

PROMPT_FILES = {
    "ONE_SHOT_DECISION": "one_shot_decision.txt",
    "ONE_SHOT_RESEARCH": "one_shot_research.txt",
    "RESEARCH_REFRESH": "research_refresh.txt",
}


class AICallError(RuntimeError):
    pass


class DeepSeek:
    def __init__(
        self,
        key: str,
        *,
        client=None,
        audit=None,
        record=None,
        input_record=None,
        model: str | None = None,
        max_tokens: int = 24000,
        temperature: float = 0.2,
        repair_attempts: int = 1,
        prompt_dir: Path | None = None,
    ):
        self.model = model or os.getenv("DEEPSEEK_MODEL", "deepseek-flash")
        self.client = client or OpenAI(
            api_key=key, base_url="https://api.deepseek.com", timeout=60, max_retries=1
        )
        self.audit = audit
        self.record = record
        self.input_record = input_record
        self.max_tokens = int(max_tokens)
        self.temperature = float(temperature)
        self.repair_attempts = max(0, min(int(repair_attempts), 1))
        self.prompt_dir = Path(prompt_dir or (ROOT / "prompts"))
        # The client owns the canonical ai_calls row.  A multi-agent wrapper must
        # not write a second row for the same provider call.
        self.records_calls = True

    def _prompt(self, mode: str) -> str:
        name = PROMPT_FILES.get(mode)
        if not name:
            raise AICallError("Unknown AI mode: " + mode)
        return (self.prompt_dir / name).read_text(encoding="utf-8")

    def _record(self, record: dict) -> None:
        if self.record:
            self.record(record)
        if self.audit:
            self.audit("ai_call", {k: v for k, v in record.items() if k != "usage"})

    def _raw(self, mode: str, attempt: int, content: str | None, finish_reason: str | None) -> None:
        if self.audit:
            self.audit(
                "ai_raw",
                {
                    "mode": mode,
                    "attempt": attempt,
                    "model": self.model,
                    "content": content,
                    "finish_reason": finish_reason,
                },
            )

    def complete(
        self,
        mode: str,
        payload: dict,
        parser,
        *,
        plan_id: str | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        timeout: float | None = None,
        role: str | None = None,
        run_id: str | None = None,
        include_raw: bool = False,
        cache_hit: bool = False,
    ):
        """Return ``(parsed, meta)``; raise :class:`AICallError` on failure.

        Multi-agent callers pass their role/model/budget/run id here, but the
        provider client and credential remain shared and are never exposed to
        the returned object or to logs.
        """
        effective_model = model or self.model
        effective_max_tokens = int(max_tokens or self.max_tokens)
        effective_temperature = self.temperature if temperature is None else float(temperature)
        system = self._prompt(mode)
        user = json.dumps(payload, default=str, ensure_ascii=False)
        if self.input_record:
            try:
                self.input_record({"mode": mode, "payload": payload})
            except Exception:  # noqa: BLE001 - audit must not authorize/block a trade
                pass
        prompt_hash = hashlib.sha256(system.encode()).hexdigest()[:32]
        input_hash = hashlib.sha256(user.encode()).hexdigest()[:32]
        started = time.monotonic()
        attempts = 0
        repair_note = ""
        last_error = None
        while True:
            attempt_started = time.monotonic()
            messages = [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ]
            if repair_note:
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Your previous answer did not satisfy the schema. Return ONLY a complete, "
                            "valid JSON object with no markdown fences and no extra text.\n"
                            "Validation error: " + repair_note
                        ),
                    }
                )
            try:
                client = self.client
                if timeout is not None and hasattr(client, "with_options"):
                    client = client.with_options(timeout=float(timeout))
                response = client.chat.completions.create(
                    model=effective_model,
                    messages=messages,
                    response_format={"type": "json_object"},
                    max_tokens=effective_max_tokens,
                    temperature=effective_temperature,
                    extra_body={"thinking": {"type": "disabled"}},
                )
            except Exception as exc:  # noqa: BLE001 - provider/transport failure
                record = {
                    "mode": mode, "model": effective_model, "prompt_hash": prompt_hash,
                    "input_hash": input_hash, "output_hash": None,
                    "prompt_tokens": None, "completion_tokens": None,
                    "latency_ms": int((time.monotonic() - started) * 1000),
                    "finish_reason": None, "outcome": "transport_error",
                    "plan_id": plan_id, "error": f"{type(exc).__name__}: {exc}",
                    "usage": None, "run_id": run_id, "agent_role": role,
                    "cached": cache_hit,
                }
                self._record(record)
                raise AICallError("DeepSeek request failed: " + type(exc).__name__) from exc
            choice = response.choices[0]
            content = choice.message.content
            finish_reason = choice.finish_reason
            output_hash = hashlib.sha256((content or "").encode()).hexdigest()[:32]
            usage = getattr(response, "usage", None)
            prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0) if usage else None
            completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0) if usage else None
            self._raw(mode, attempts, content, finish_reason)
            try:
                if finish_reason != "stop":
                    raise ValueError("Incomplete model response: " + str(finish_reason))
                if not content or not content.strip():
                    raise ValueError("Empty model response")
                parsed = parser(content)
                elapsed = int((time.monotonic() - started) * 1000)
                meta = {
                    "mode": mode, "model": effective_model,
                    "resolved_model": getattr(response, "model", None),
                    "latency_ms": elapsed, "attempts": attempts + 1,
                    "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                    "finish_reason": finish_reason, "prompt_hash": prompt_hash,
                    "input_hash": input_hash, "output_hash": output_hash,
                    "repair_used": bool(repair_note), "run_id": run_id,
                    "agent_role": role, "cached": cache_hit,
                }
                if include_raw:
                    meta["raw"] = content
                self._record(
                    {
                        "mode": mode, "model": effective_model, "prompt_hash": prompt_hash,
                        "input_hash": input_hash, "output_hash": output_hash,
                        "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                        "latency_ms": elapsed, "finish_reason": finish_reason,
                        "outcome": "ok", "plan_id": plan_id, "error": None,
                        "usage": {
                            "prompt_tokens": prompt_tokens,
                            "completion_tokens": completion_tokens,
                            "attempts": attempts + 1,
                        },
                        "run_id": run_id, "agent_role": role, "cached": cache_hit,
                    }
                )
                return parsed, meta
            except (ValueError, TypeError) as exc:
                last_error = exc
                self._record(
                    {
                        "mode": mode, "model": effective_model, "prompt_hash": prompt_hash,
                        "input_hash": input_hash, "output_hash": output_hash,
                        "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                        "latency_ms": int((time.monotonic() - attempt_started) * 1000),
                        "finish_reason": finish_reason, "outcome": "schema_error",
                        "plan_id": plan_id, "error": str(exc)[:2000], "usage": None,
                        "run_id": run_id, "agent_role": role, "cached": cache_hit,
                    }
                )
                if attempts >= self.repair_attempts:
                    raise AICallError("DeepSeek response failed validation: " + str(exc)) from exc
                attempts += 1
                repair_note = (str(exc)[:1500] + "\nPrevious output (truncated): " + (content or "")[:12000])
