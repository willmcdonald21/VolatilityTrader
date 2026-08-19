from __future__ import annotations

import math
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


def resample_bars(bars: list[Bar], bucket_minutes: int) -> list[Bar]:
    """Downsample 1-minute bars into `bucket_minutes`-wide OHLCV bars,
    aligned to absolute wall-clock boundaries (e.g. :00/:05/:10 for a
    5-minute bucket) rather than to the first bar's own timestamp.

    Used for multi-timeframe confirmation (checking MACD/candle-shape state
    on a slower timeframe than the one used for entry) purely by
    downsampling bar history this bot already has -- no second IB bar
    subscription needed. The trailing bucket may be partial (still forming)
    if the caller's `bars` don't yet span a full period; that's expected,
    same as how a live 5-minute chart's current candle is partial too.
    """
    if bucket_minutes <= 0 or not bars:
        return []
    buckets: dict[int, list[Bar]] = {}
    for bar in bars:
        bucket_key = int(bar.time.timestamp() // 60 // bucket_minutes)
        buckets.setdefault(bucket_key, []).append(bar)
    resampled = []
    for key in sorted(buckets):
        group = buckets[key]
        resampled.append(
            Bar(
                time=group[0].time,
                open=group[0].open,
                high=max(b.high for b in group),
                low=min(b.low for b in group),
                close=group[-1].close,
                volume=sum(b.volume for b in group),
            )
        )
    return resampled


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


def round_number_increment(price: float) -> float:
    """Psychological price-level granularity: half-dollar increments below
    $10, whole-dollar at/above $10 -- source material's explicit split
    (low-priced stocks cluster orders at half-dollar levels; above ~$10
    the granularity that matters shifts to whole dollars)."""
    return 0.5 if price < 10.0 else 1.0


def crossed_round_number(prior_price: float, current_price: float) -> bool:
    """True if price just crossed upward through a psychological
    round-number level it was still below -- a breakout through horizontal
    resistance at a level where resting sell/take-profit orders cluster
    (source material's own strongest example: $1.00 for low-priced
    stocks, "very hard... to break and hold over $1"). Granularity is
    based on `prior_price` (the level being broken *from*), not the
    post-breakout price, so a move from $9.80 to $10.20 is correctly
    evaluated against the $0.50 grid it started on, not the $1 grid it
    ends on."""
    if current_price <= prior_price:
        return False
    increment = round_number_increment(prior_price)
    prior_level = math.floor(prior_price / increment)
    current_level = math.floor(current_price / increment)
    return current_level > prior_level


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


def candle_strength(bar: Bar) -> float:
    """Signed, wick-weighted candle conviction in [-1, 1]: (close - open) /
    (high - low). +1.0 is a full-bodied green candle (open=low, close=high,
    the single strongest bullish shape); -1.0 the mirror-image full-bodied
    red candle. A candle's color alone understates/overstates conviction --
    a green candle with a large upper wick scores well below +1.0 even
    though it's colored green, and a red candle with a large lower wick
    scores well above -1.0 -- which is the point: wick structure, not raw
    color, should drive how much a candle is trusted. Returns 0.0 for a
    zero-range bar (doji-like indecision, or malformed data) rather than
    dividing by zero.
    """
    range_ = bar.high - bar.low
    if range_ <= 0:
        return 0.0
    return (bar.close - bar.open) / range_


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


def is_bottoming_tail(bar: Bar, wick_ratio: float = 2.0) -> bool:
    """Mirror of is_topping_tail: long lower wick relative to the candle
    body -- sellers pushed lower but were rejected and price recovered
    ("hammering out the base"). A soft bullish confirmation signal on a
    pullback's low bar, not a hard entry gate -- color-independent, same
    as the source material's own framing (bullish regardless of whether
    the small body itself closed red or green)."""
    body = abs(bar.close - bar.open)
    if body <= 0:
        return False
    upper_wick = bar.high - max(bar.open, bar.close)
    lower_wick = min(bar.open, bar.close) - bar.low
    return lower_wick >= wick_ratio * body and lower_wick > upper_wick


def is_momentum_exhausted(bars: list[Bar], lookback: int = 3) -> bool:
    """Sequential shrinking green-candle bodies combined with shrinking
    volume across the most recent `lookback` bars -- trend exhaustion, a
    warning sign a reversal may be imminent even with no single
    bearish-shaped candle yet. Requires BOTH signals jointly: the source
    material explicitly treats shrinking body size alone (with volume
    still rising) as a weaker, lower-confidence version of this signal,
    not the same thing -- still-growing volume on a shrinking body can
    still reflect genuine, if slowing, buying interest."""
    if len(bars) < lookback:
        return False
    recent = bars[-lookback:]
    if not all(b.close > b.open for b in recent):
        return False
    bodies = [b.close - b.open for b in recent]
    volumes = [b.volume for b in recent]
    shrinking_bodies = all(bodies[i] < bodies[i - 1] for i in range(1, len(bodies)))
    shrinking_volume = all(volumes[i] < volumes[i - 1] for i in range(1, len(volumes)))
    return shrinking_bodies and shrinking_volume


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
