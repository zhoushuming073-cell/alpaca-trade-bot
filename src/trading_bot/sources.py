"""DeepSeek official Search & Fetch tools plus research source adapters.

Official endpoints (2026-11 docs):
  POST https://api.deepseek.com/v1/tools/search
  POST https://api.deepseek.com/v1/tools/fetch

The API key never appears in logs/events.  Retrieval failures are explicit and
never turned into invented news or filings.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests

from .research import SourceUnavailable

BASE_URL = "https://api.deepseek.com"


class DeepSeekTools:
    def __init__(
        self,
        api_key: str,
        *,
        session=None,
        timeout: float = 20.0,
        max_retries: int = 2,
        audit=None,
        clock=None,
    ):
        if not api_key:
            raise ValueError("DeepSeek API key missing")
        self._key = api_key
        self.session = session or requests.Session()
        self.timeout = float(timeout)
        self.max_retries = max(0, min(int(max_retries), 4))
        self.audit = audit
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _audit(self, payload):
        if self.audit:
            try:
                self.audit("source_call", payload)
            except Exception:  # noqa: BLE001 - audit is not control flow
                pass

    def _post(self, path: str, body: dict) -> dict:
        headers = {
            "Authorization": "Bearer " + self._key,
            "Content-Type": "application/json",
        }
        last_error = None
        for attempt in range(self.max_retries + 1):
            started = time.monotonic()
            try:
                response = self.session.post(
                    BASE_URL + path, headers=headers, json=body,
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                last_error = exc
                self._audit({
                    "endpoint": path, "attempt": attempt + 1, "outcome": "transport_error",
                    "latency_ms": int((time.monotonic() - started) * 1000),
                    "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                })
                if attempt >= self.max_retries:
                    raise SourceUnavailable("DeepSeek tools transport error") from exc
                time.sleep(0.6 * (2 ** attempt))
                continue
            status = response.status_code
            if status in (408, 429, 500, 502, 503, 504) and attempt < self.max_retries:
                retry_after = response.headers.get("Retry-After")
                delay = 0.6 * (2 ** attempt)
                if retry_after:
                    try:
                        delay = max(delay, min(float(retry_after), 5.0))
                    except ValueError:
                        pass
                self._audit({
                    "endpoint": path, "attempt": attempt + 1, "outcome": "retryable_http",
                    "status": status, "latency_ms": int((time.monotonic() - started) * 1000),
                })
                time.sleep(delay)
                continue
            if status != 200:
                message = ""
                try:
                    message = str((response.json().get("error") or {}).get("message") or "")
                except Exception:  # noqa: BLE001
                    message = response.text[:200]
                self._audit({
                    "endpoint": path, "attempt": attempt + 1, "outcome": "http_error",
                    "status": status, "error": message[:300],
                    "latency_ms": int((time.monotonic() - started) * 1000),
                })
                if status == 402:
                    raise SourceUnavailable("DeepSeek account has insufficient balance")
                raise SourceUnavailable(f"DeepSeek tools HTTP {status}")
            try:
                data = response.json()
            except ValueError as exc:
                raise SourceUnavailable("DeepSeek tools returned invalid JSON") from exc
            if not isinstance(data, dict):
                raise SourceUnavailable("DeepSeek tools returned a non-object")
            self._audit({
                "endpoint": path, "attempt": attempt + 1, "outcome": "ok", "status": status,
                "latency_ms": int((time.monotonic() - started) * 1000),
                "result_count": len(data.get("results") or []) if isinstance(data.get("results"), list) else None,
            })
            return data
        raise SourceUnavailable("DeepSeek tools request failed") from last_error

    def search(self, query: str, *, count: int = 5, need_content: bool = True) -> list[dict]:
        query = str(query or "").strip()
        if not 1 <= len(query) <= 128:
            raise ValueError("search query must be 1..128 characters")
        count = max(1, min(int(count), 50))
        data = self._post("/v1/tools/search", {
            "query": query, "count": count, "need_content": bool(need_content),
        })
        results = data.get("results")
        if not isinstance(results, list):
            raise SourceUnavailable("Search response missing results")
        cleaned = []
        for item in results[:count]:
            if not isinstance(item, dict) or not item.get("url"):
                continue
            cleaned.append({
                "title": str(item.get("title") or ""),
                "url": str(item["url"]),
                "snippet": str(item.get("snippet") or ""),
                "summary": str(item.get("summary") or ""),
                "content": str(item.get("content") or ""),
                "site_name": item.get("site_name"),
                "date": item.get("date"),
                "authority": item.get("authority"),
                "rerank_score": item.get("rerank_score"),
            })
        return cleaned

    def fetch(self, url: str) -> dict:
        parsed = urlparse(str(url or ""))
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("fetch requires a plain HTTP(S) URL with a host")
        if parsed.username or parsed.password:
            raise ValueError("credentials in URL are not allowed")
        data = self._post("/v1/tools/fetch", {"url": str(url)})
        return {
            "title": str(data.get("title") or ""),
            "content": str(data.get("content") or ""),
        }

    def capabilities(self) -> dict:
        return {
            "deepseek_search": "CONFIGURED",
            "deepseek_fetch": "CONFIGURED",
            "base_url": BASE_URL,
            "reason": "official Search/Fetch endpoints configured; successful calls are recorded",
        }

    def probe(self) -> dict:
        """One small real call used by CLI to verify credentials/balance."""
        started = time.monotonic()
        try:
            results = self.search("DeepSeek API", count=1, need_content=False)
            return {
                "status": "VERIFIED" if results else "EMPTY",
                "results": len(results),
                "latency_ms": int((time.monotonic() - started) * 1000),
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "status": "UNAVAILABLE",
                "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                "latency_ms": int((time.monotonic() - started) * 1000),
            }


class DeepSeekResearchSources:
    """Search plus Fetch, normalized into research evidence records."""

    def __init__(self, tools: DeepSeekTools, *, fetch_top: int = 2, clock=None):
        self.tools = tools
        self.fetch_top = max(0, min(int(fetch_top), 5))
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def capabilities(self):
        return self.tools.capabilities()

    def _kind(self, url: str, published_at) -> str:
        lower = url.lower()
        if "sec.gov" in lower:
            return "filing"
        if "investor" in lower or "/ir/" in lower or "ir." in lower:
            return "ir"
        return "news" if published_at else "other"

    def _parse_date(self, value):
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()

    def search(self, symbol: str):
        query = (
            f"{symbol} business revenue profitability competition risks "
            f"latest earnings catalyst investor relations filing"
        )
        results = self.tools.search(query, count=6, need_content=True)
        now = self.clock()
        evidence = []
        for item in results:
            url = item.get("url")
            if not url:
                continue
            published = self._parse_date(item.get("date"))
            content = item.get("content") or ""
            summary = item.get("summary") or item.get("snippet") or ""
            verified = bool(content)
            # Fetch the top pages explicitly so the source adapter exercises
            # both official APIs and can verify full-text retrieval.
            if len(evidence) < self.fetch_top:
                try:
                    page = self.tools.fetch(url)
                    if page.get("content"):
                        content = page["content"]
                        summary = summary or page.get("title") or ""
                        verified = True
                        if page.get("title"):
                            item["title"] = page["title"]
                except Exception as exc:  # noqa: BLE001
                    if self.tools.audit:
                        self.tools.audit("source_fetch_error", {
                            "symbol": symbol, "url": url,
                            "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                        })
            evidence.append({
                "kind": self._kind(url, published),
                "source_url": url,
                "title": item.get("title") or "",
                "published_at_utc": published,
                "retrieved_at_utc": now.isoformat(),
                "available_at_utc": published or now.isoformat(),
                "verified": verified,
                "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest()[:32] if content else None,
                "summary": summary[:2000],
            })
        return evidence

    def fetch(self, url: str):
        page = self.tools.fetch(url)
        return {
            "title": page.get("title") or "",
            "content": page.get("content") or "",
            "retrieved_at_utc": self.clock().isoformat(),
            "content_hash": hashlib.sha256((page.get("content") or "").encode("utf-8")).hexdigest()[:32],
        }
