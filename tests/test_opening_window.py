from datetime import datetime, timezone

import pytest

from trading_bot.market_data import SessionPhase, session_info


def at(value, bundle):
    return session_info(datetime.fromisoformat(value), bundle.execution)


@pytest.mark.parametrize("value,phase,active,can_submit", [
    ("2026-11-05T13:55:00+00:00", SessionPhase.CLOSED, False, False),
    ("2026-11-05T14:05:00+00:00", SessionPhase.PREMARKET, False, False),
    ("2026-11-05T14:29:59+00:00", SessionPhase.PREMARKET, False, False),
    ("2026-11-05T14:30:00+00:00", SessionPhase.ACTIVE, True, True),
    ("2026-11-05T14:50:00+00:00", SessionPhase.ACTIVE, True, True),
    ("2026-11-05T14:59:29+00:00", SessionPhase.ACTIVE, True, True),
    ("2026-11-05T14:59:30+00:00", SessionPhase.ACTIVE, True, False),
    ("2026-11-05T15:00:00+00:00", SessionPhase.CLOSED, False, False),
    ("2026-11-05T17:00:00+00:00", SessionPhase.CLOSED, False, False),
])
def test_session_phases(value, phase, active, can_submit, bundle):
    info = at(value, bundle)
    assert info.phase == phase
    assert info.active is active
    assert info.can_submit is can_submit


def test_summer_open_and_dst(bundle):
    info = at("2026-07-06T13:35:00+00:00", bundle)
    assert info.phase == SessionPhase.ACTIVE
    assert info.market_open.isoformat() == "2026-07-06T13:30:00+00:00"
    assert info.can_submit is True
    assert at("2026-07-06T13:59:30+00:00", bundle).can_submit is False


def test_holiday_and_weekend_have_no_session(bundle):
    for value in ("2026-11-26T15:00:00+00:00", "2026-11-07T15:00:00+00:00"):
        info = at(value, bundle)
        assert info.phase is SessionPhase.NO_SESSION
        assert info.market_open is None
        assert info.can_submit is False


def test_early_close_still_has_only_first_30_minutes(bundle):
    info = at("2026-11-27T14:35:00+00:00", bundle)
    assert info.phase == SessionPhase.ACTIVE
    assert info.market_close.isoformat() == "2026-11-27T18:00:00+00:00"
    late = at("2026-11-27T15:05:00+00:00", bundle)
    assert late.phase == SessionPhase.CLOSED
    assert late.can_submit is False


def test_submit_cutoff_is_10am_minus_30_seconds(bundle):
    info = at("2026-11-05T14:35:00+00:00", bundle)
    assert info.submit_cutoff.isoformat() == "2026-11-05T14:59:30+00:00"
    assert info.active_end.isoformat() == "2026-11-05T15:00:00+00:00"
