"""Bounded retries for reads only. No POST or DELETE retry wrapper."""
import time
import requests


def read_retry(operation, audit=None):
    for attempt in range(3):
        try:
            return operation()
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            retryable = isinstance(exc, (requests.Timeout, requests.ConnectionError)) or status in (429, 500, 502, 503, 504)
            if not retryable or attempt == 2:
                try:
                    exc._bot_retries = attempt
                except AttributeError:
                    pass
                raise
            if audit:
                audit("network_retry", {"type": type(exc).__name__, "reason": str(exc),
                                        "retried": True, "attempt": attempt + 1, "outcome": "retry_read"})
            time.sleep(.25 * 2 ** attempt)
