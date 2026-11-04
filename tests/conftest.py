import socket
import pytest

@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args,**kwargs):
        raise AssertionError("Network forbidden in offline tests")
    monkeypatch.setattr(socket.socket,"connect",blocked)
    monkeypatch.setattr(socket,"create_connection",blocked)

@pytest.fixture
def risk():
    from trading_bot.settings import config
    return config()[2]

@pytest.fixture
def store(tmp_path):
    from trading_bot.storage import Store
    s=Store(tmp_path/"test.sqlite")
    yield s
    s.db.close()
