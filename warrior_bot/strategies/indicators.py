from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class Bar:
    """Minimal OHLCV bar, decoupled from ib_async so pattern-detection
    functions can be unit-tested against synthetic fixtures with no IBKR
    dependency."""

    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float

    @property
    def typical_price(self) -> float:
        return (self.high + self.low + self.close) / 3.0


def bars_from_ib(ib_bars) -> list[Bar]:
    return [
        Bar(time=b.date, open=b.open, high=b.high, low=b.low, close=b.close, volume=b.volume)
        for b in ib_bars
    ]


def vwap(bars: list[Bar]) -> float | None:
    """Cumulative session VWAP over the given bars. Caller decides which
    bars belong to "the session" (e.g. include or exclude pre-market)."""
    if not bars:
        return None
    total_pv = sum(b.typical_price * b.volume for b in bars)
    total_v = sum(b.volume for b in bars)
    if total_v <= 0:
        return None
    return total_pv / total_v


def relative_volume(volume_so_far: float, avg_daily_volume: float, elapsed_fraction: float) -> float | None:
    if avg_daily_volume <= 0 or elapsed_fraction <= 0:
        return None
    expected_by_now = avg_daily_volume * elapsed_fraction
    if expected_by_now <= 0:
        return None
    return volume_so_far / expected_by_now


def opening_range(bars: list[Bar], lookback_bars: int) -> tuple[float, float] | None:
    """(high, low) over the most recent `lookback_bars` bars."""
    window = bars[-lookback_bars:] if lookback_bars > 0 else bars
    if not window:
        return None
    return max(b.high for b in window), min(b.low for b in window)


def gap_pct(prior_close: float, current_price: float) -> float | None:
    if prior_close is None or prior_close <= 0:
        return None
    return (current_price - prior_close) / prior_close * 100.0


def is_red_to_green(prior_close: float, current_price: float, prior_price: float) -> bool:
    """True on the bar where price crosses from below prior_close (red) to
    at/above prior_close (green)."""
    return prior_price < prior_close <= current_price


def swing_points(bars: list[Bar], window: int = 2) -> list[tuple[int, str, float]]:
    """Simple local swing high/low detector.

    Returns a list of (index, 'high'|'low', price) for bars that are a
    strict local extreme over +/- `window` bars on each side. Used by
    bull-flag and ABCD detectors to find the spike top / pullback low
    without hand-rolling extrema logic per strategy.
    """
    points: list[tuple[int, str, float]] = []
    n = len(bars)
    for i in range(window, n - window):
        segment = bars[i - window : i + window + 1]
        center = bars[i]
        if center.high == max(b.high for b in segment) and center.high > bars[i - window].high:
            points.append((i, "high", center.high))
        if center.low == min(b.low for b in segment) and center.low < bars[i - window].low:
            points.append((i, "low", center.low))
    return points


def average_true_range(bars: list[Bar], period: int = 14) -> float | None:
    if len(bars) < 2:
        return None
    window = bars[-(period + 1):]
    trs = []
    for prev, cur in zip(window, window[1:]):
        tr = max(
            cur.high - cur.low,
            abs(cur.high - prev.close),
            abs(cur.low - prev.close),
        )
        trs.append(tr)
    if not trs:
        return None
    return sum(trs) / len(trs)
