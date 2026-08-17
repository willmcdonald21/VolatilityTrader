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


def _ema_series(values: list[float], period: int) -> list[float]:
    """Full EMA series (not just the latest value), seeded with a simple
    average over the first `period` values. series[i] is the EMA as of
    values[period - 1 + i]. Needed by macd(), which requires an EMA *of*
    the MACD line's own history, not just a single final value."""
    if len(values) < period:
        return []
    multiplier = 2.0 / (period + 1)
    series = [sum(values[:period]) / period]
    for value in values[period:]:
        series.append((value - series[-1]) * multiplier + series[-1])
    return series


def ema(bars: list[Bar], period: int) -> float | None:
    """Exponential moving average of closes, seeded with a simple average
    over the first `period` closes (standard EMA warm-up)."""
    series = _ema_series([b.close for b in bars], period)
    return series[-1] if series else None


def macd(bars: list[Bar], fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[float, float] | None:
    """(macd_line, signal_line) as of the latest bar, or None if there
    isn't enough history yet -- callers should treat None as "unknown",
    never as a bearish reading (insufficient warm-up data isn't the same
    as a bearish crossover)."""
    closes = [b.close for b in bars]
    fast_series = _ema_series(closes, fast)
    slow_series = _ema_series(closes, slow)
    if not fast_series or not slow_series:
        return None
    offset = slow - fast
    macd_line = [f - s for f, s in zip(fast_series[offset:], slow_series)]
    signal_series = _ema_series(macd_line, signal)
    if not signal_series:
        return None
    return macd_line[-1], signal_series[-1]


def is_topping_tail(bar: Bar, wick_ratio: float = 2.0) -> bool:
    """Long upper wick relative to the candle body -- buyers pushed higher
    but were rejected. One of the three exit indicators from the source
    material that's directly OHLCV-computable (the other two -- a large
    Level 2 seller, decelerating tape -- need order-book data this bot
    doesn't have)."""
    body = abs(bar.close - bar.open)
    if body <= 0:
        return False
    upper_wick = bar.high - max(bar.open, bar.close)
    lower_wick = min(bar.open, bar.close) - bar.low
    return upper_wick >= wick_ratio * body and upper_wick > lower_wick


def is_red_after_green(prior_bar: Bar, current_bar: Bar) -> bool:
    """A red candle immediately following a green one -- momentum stalling
    or reversing, one bar after a push higher."""
    return prior_bar.close > prior_bar.open and current_bar.close < current_bar.open


def is_lower_low(current_bar: Bar, prior_bar: Bar) -> bool:
    """The first candle whose low undercuts the previous candle's low --
    described across the source material as the cleanest, most mechanical
    full-exit confirmation trigger. Meant to be checked only once a
    position is already profitable/trend-established, not from the first
    bar after entry -- callers are responsible for that gating."""
    return current_bar.low < prior_bar.low


def is_high_volume_red_bar(bar: Bar, avg_recent_volume: float, multiple: float = 2.0) -> bool:
    """A red candle with a burst of volume well above the recent average --
    aggressive selling, not just a normal light-volume pullback."""
    if avg_recent_volume <= 0:
        return False
    return bar.close < bar.open and bar.volume >= avg_recent_volume * multiple


def trailing_candidate(
    last_price: float | None,
    ema_9: float | None,
    atr: float | None,
    method: str,
    atr_multiple: float,
) -> float | None:
    """Candidate long-side trailing-stop price for the given method. Caller
    is responsible for ratcheting (never loosening an existing stop) — this
    only computes where the stop *could* move to on this bar."""
    if method == "ema":
        return ema_9
    if last_price is None or atr is None:
        return None
    return last_price - atr * atr_multiple


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
