import json

import pytest

import trading_bot.sources as sources_module
from trading_bot.research import SourceUnavailable
from trading_bot.sources import DeepSeekResearchSources, DeepSeekTools


class Response:
    def __init__(self, status, data, headers=None):
        self.status_code = status
        self._data = data
        self.headers = headers or {}
        self.text = json.dumps(data)

    def json(self):
        return self._data


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, *, headers, json, timeout):
        self.calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return self.responses.pop(0)


def test_search_normalizes_and_fetch_uses_official_endpoints(monkeypatch):
    session = Session([
        Response(200, {"results": [{
            "title": "Example", "url": "https://example.com/a", "snapshot": "",
            "snippet": "s", "summary": "summary", "content": "body",
            "site_name": "Example", "date": "2026-01-02T00:00:00+00:00",
            "authority": 3, "rerank_score": 2.0,
        }]}),
        Response(200, {"title": "Fetched", "content": "# body"}),
    ])
    tools = DeepSeekTools("test-key", session=session)
    results = tools.search("CRWV earnings", count=1, need_content=True)
    assert results[0]["url"] == "https://example.com/a"
    assert session.calls[0]["url"] == "https://api.deepseek.com/v1/tools/search"
    assert session.calls[0]["headers"]["Authorization"] == "Bearer test-key"
    page = tools.fetch("https://example.com/a")
    assert page["title"] == "Fetched"
    assert session.calls[1]["url"] == "https://api.deepseek.com/v1/tools/fetch"


def test_retries_transient_status_without_leaking_key(monkeypatch):
    session = Session([
        Response(429, {"error": {"message": "rate"}}, {"Retry-After": "0"}),
        Response(200, {"results": [{"url": "https://example.com", "title": "x"}]}),
    ])
    monkeypatch.setattr(sources_module.time, "sleep", lambda seconds: None)
    events = []
    tools = DeepSeekTools("secret-key", session=session, max_retries=1, audit=lambda k, p: events.append((k, p)))
    results = tools.search("x", count=1, need_content=False)
    assert results[0]["url"] == "https://example.com"
    assert len(session.calls) == 2
    assert all("secret-key" not in json.dumps(event) for event in events)


def test_non_retryable_http_error_is_explicit():
    session = Session([Response(400, {"error": {"message": "bad query"}})])
    tools = DeepSeekTools("test-key", session=session, max_retries=2)
    with pytest.raises(SourceUnavailable):
        tools.search("x", count=1, need_content=False)


def test_source_adapter_marks_fetched_content_verified_and_keeps_dates():
    class Tools:
        def __init__(self):
            self.audit = None

        def search(self, query, *, count, need_content):
            assert len(query) <= 128
            return [{
                "title": "10-Q", "url": "https://www.sec.gov/example",
                "snippet": "", "summary": "summary", "content": "search body",
                "date": "2026-01-02T00:00:00+00:00", "authority": 4,
            }]

        def fetch(self, url):
            return {"title": "10-Q full", "content": "full body"}

    adapter = DeepSeekResearchSources(Tools(), fetch_top=1)
    evidence = adapter.search("CRWV")
    assert evidence[0]["verified"] is True
    assert evidence[0]["kind"] == "filing"
    assert evidence[0]["published_at_utc"] == "2026-01-02T00:00:00+00:00"
    assert evidence[0]["content_hash"]
