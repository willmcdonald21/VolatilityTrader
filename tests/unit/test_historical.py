from __future__ import annotations

import asyncio
from datetime import date, timedelta
from types import SimpleNamespace

from warrior_bot.broker.historical import fetch_prior_close
from warrior_bot.utils.time_utils import now_eastern


class FakeIB:
    def __init__(self, bars):
        self._bars = bars

    async def reqHistoricalDataAsync(self, *args, **kwargs):
        return self._bars


def bar(day: date, close: float):
    return SimpleNamespace(date=day, close=close)


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_prior_close_skips_todays_forming_bar():
    # During RTH IBKR includes a bar for today -- the prior close is the one
    # before it.
    today = now_eastern().date()
    bars = [
        bar(today - timedelta(days=2), 1.00),
        bar(today - timedelta(days=1), 2.00),
        bar(today, 3.00),
    ]

    assert run(fetch_prior_close(FakeIB(bars), object())) == 2.00


def test_prior_close_uses_last_session_when_today_has_no_bar_yet():
    # Pre-market -- when this bot does most of its trading -- IBKR has no
    # bar for today at all. Taking the second-to-last bar here (the old
    # behaviour) returned the close from *two* sessions ago.
    today = now_eastern().date()
    bars = [
        bar(today - timedelta(days=2), 1.00),
        bar(today - timedelta(days=1), 2.00),
    ]

    assert run(fetch_prior_close(FakeIB(bars), object())) == 2.00


def test_prior_close_handles_a_weekend_gap():
    today = now_eastern().date()
    bars = [
        bar(today - timedelta(days=6), 1.00),
        bar(today - timedelta(days=4), 2.00),
    ]

    assert run(fetch_prior_close(FakeIB(bars), object())) == 2.00


def test_prior_close_is_none_when_only_todays_bar_exists():
    today = now_eastern().date()

    assert run(fetch_prior_close(FakeIB([bar(today, 3.00)]), object())) is None


def test_prior_close_is_none_with_no_bars():
    assert run(fetch_prior_close(FakeIB([]), object())) is None
