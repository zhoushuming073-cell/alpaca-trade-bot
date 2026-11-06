from datetime import timedelta


def test_heartbeat_is_independent_of_manual_pause_and_stales(store, clock):
    store.pause(True)
    assert store.web_connected()
    store.web_heartbeat()
    assert store.paused()
    assert store.web_connected()

    clock.advance(31)
    assert not store.web_connected()

    store.web_heartbeat()
    assert store.web_connected()
    assert store.web_reconnect_required()
    assert store.paused()


def test_web_disconnect_requires_reconnect(store):
    store.web_disconnect()
    assert not store.web_connected()
    assert store.web_reconnect_required()
