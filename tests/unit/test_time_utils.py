from __future__ import annotations

from datetime import datetime

from warrior_bot.utils.time_utils import EASTERN, is_active_session


def _et(hour, minute=0):
    return datetime(2026, 9, 16, hour, minute, tzinfo=EASTERN)


def test_is_active_session_true_at_pre_market_open():
    assert is_active_session(_et(4, 0)) is True


def test_is_active_session_true_during_regular_hours():
    assert is_active_session(_et(12, 0)) is True


def test_is_active_session_true_just_before_after_hours_close():
    assert is_active_session(_et(19, 59)) is True


def test_is_active_session_false_at_after_hours_close():
    assert is_active_session(_et(20, 0)) is False


def test_is_active_session_false_overnight():
    assert is_active_session(_et(2, 0)) is False


def test_is_active_session_false_just_before_pre_market_open():
    assert is_active_session(_et(3, 59)) is False
