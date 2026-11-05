import socket
from datetime import datetime, timezone

import pytest

from trading_bot.market_data import session_info
from trading_bot.settings import load_config
from trading_bot.storage import Store

ACTIVE_NOW = datetime(2026, 11, 5, 14, 35, 10, tzinfo=timezone.utc)
PREMARKET_NOW = datetime(2026, 11, 5, 14, 5, 10, tzinfo=timezone.utc)


class MutableClock:
    def __init__(self, value=ACTIVE_NOW):
        self.value = value

    def __call__(self):
        return self.value

    def set(self, value):
        self.value = value

    def advance(self, seconds):
        from datetime import timedelta

        self.value = self.value + timedelta(seconds=seconds)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Network forbidden in offline tests")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


@pytest.fixture
def bundle():
    return load_config()


@pytest.fixture
def clock():
    return MutableClock(ACTIVE_NOW)


@pytest.fixture
def store(tmp_path, clock):
    s = Store(tmp_path / "test.sqlite", clock=clock, instance_id="test-instance")
    yield s
    s.close()


@pytest.fixture
def risk(bundle):
    return bundle.risk


@pytest.fixture
def session(bundle, clock):
    return session_info(clock(), bundle.execution)


@pytest.fixture
def premarket_session(bundle, clock):
    return session_info(PREMARKET_NOW, bundle.execution)
