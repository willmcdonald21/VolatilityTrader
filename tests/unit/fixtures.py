from __future__ import annotations

from datetime import datetime, timedelta, timezone

from warrior_bot.strategies.indicators import Bar

BASE_TIME = datetime(2026, 1, 5, 9, 30, tzinfo=timezone.utc)


def make_bars(specs: list[tuple[float, float, float, float, float]], start: datetime = BASE_TIME) -> list[Bar]:
    """specs: list of (open, high, low, close, volume) -> one-minute-spaced Bars."""
    bars = []
    for i, (o, h, l, c, v) in enumerate(specs):
        bars.append(Bar(time=start + timedelta(minutes=i), open=o, high=h, low=l, close=c, volume=v))
    return bars


def flat_bars(price: float, volume: float, count: int) -> list[tuple[float, float, float, float, float]]:
    return [(price, price, price, price, volume) for _ in range(count)]
