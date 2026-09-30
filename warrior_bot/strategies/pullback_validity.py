from __future__ import annotations

from dataclasses import dataclass

from warrior_bot.config import PullbackQualityConfig
from warrior_bot.strategies.base_strategy import SymbolContext
from warrior_bot.strategies.indicators import (
    Bar,
    has_rising_volume_on_advance,
    is_high_volume_red_bar,
    is_topping_tail,
    macd,
    resample_bars,
)


@dataclass(frozen=True)
class PullbackValidity:
    valid: bool
    reason: str | None = None


def _slice_start_index(ctx: SymbolContext, bars: list[Bar]) -> int | None:
    """Where `bars` begins within `ctx.bars`.

    Both callers pass slices of `ctx.bars`, so the Bar objects are identical
    by identity. Scanned from the end because a pullback is by definition
    recent. None means the caller synthesised its own bars (only tests do),
    in which case the point-in-time lookups fall back to current values.
    """
    if not bars:
        return None
    first = bars[0]
    for i in range(len(ctx.bars) - 1, -1, -1):
        if ctx.bars[i] is first:
            return i
    return None


def _trim_to_advance(up_move_bars: list[Bar]) -> list[Bar]:
    """The actual advance, not the whole lookback window.

    Callers hand over everything from the start of a 40-bar window up to the
    peak, so on a symbol that chopped sideways for half an hour before a
    three-bar rip, `up_move_bars` was ~37 bars of chop plus the advance. Two
    consequences: has_rising_volume_on_advance was asked whether volume rose
    across the chop (it hadn't, so a genuine advance got rejected), and the
    "pullback volume lighter than the up-move" comparison was measured
    against 40 bars of accumulated volume, which a 3-bar pullback can
    essentially never exceed -- so that gate passed almost unconditionally.

    The advance is the trailing run of strictly higher highs ending at the
    peak. Not "everything after the window's lowest low": chop routinely
    dips below the level the advance later starts from, which puts the
    lowest low back in the chop and trims nothing. Not tolerant of a single
    non-higher high either -- allowing one lets a flat stretch of equal
    highs back in, and the failure mode of being strict is a SHORTER
    advance, i.e. the volume trend is read off the freshest bars, which is
    where the signal is anyway.
    """
    if len(up_move_bars) < 2:
        return up_move_bars
    start = len(up_move_bars) - 1
    while start > 0 and up_move_bars[start - 1].high < up_move_bars[start].high:
        start -= 1
    advance = up_move_bars[start:]
    # A one-bar "advance" carries no volume trend; keep the last two so the
    # rising-volume check has something to compare and the aggregate volume
    # comparison is not trivially small.
    return advance if len(advance) >= 2 else up_move_bars[-2:]


def validate_pullback(
    pullback_bars: list[Bar],
    up_move_bars: list[Bar],
    ctx: SymbolContext,
    config: PullbackQualityConfig | None = None,
) -> PullbackValidity:
    """Shared pullback-quality gate for bull_flag and abcd -- both are
    "spike then pullback then breakout" patterns, and the source material's
    validity rules are about pullback quality generically, not specific to
    either strategy's own consolidation-depth logic.

    Base checks, all framed as hard invalidations in the source material
    ("I would not take that trade" / "do not take the breakout entry"):
    rising volume on the preceding advance, lighter volume on the pullback
    than that advance, holds above VWAP, holds above the 9 EMA (by close,
    not by low -- a brief wick through the EMA that closes back above it
    is tolerated as noise), and a non-bearish MACD. A gate is only ever
    enforced when its underlying data is actually available --
    insufficient warm-up history (e.g. MACD needs ~34 bars) means
    "unknown", not "invalid", so it never blocks a signal on its own.

    Additional "dip or dump" dump-checklist checks (config-gated, all
    optional-on-missing-data the same way): a topping tail or high-volume
    red bar within the pullback itself, a precise pairwise volume
    comparison against the specific green candle immediately preceding the
    pullback, plus a multi-timeframe veto -- the same MACD/topping-tail
    checks recomputed on 5-minute bars resampled from `ctx.bars` -- since a
    clean 1-minute pullback can still sit under a deteriorating 5-minute
    trend.
    """
    if config is None:
        config = PullbackQualityConfig()

    if not pullback_bars or not up_move_bars:
        return PullbackValidity(True)

    up_move_bars = _trim_to_advance(up_move_bars)
    pullback_start = _slice_start_index(ctx, pullback_bars)

    pullback_volume = sum(b.volume for b in pullback_bars)
    up_move_volume = sum(b.volume for b in up_move_bars)
    if up_move_volume > 0 and pullback_volume >= up_move_volume:
        return PullbackValidity(False, "pullback volume not lighter than the preceding up-move")

    if config.require_pullback_lighter_than_prior_green_bar:
        # Precise pairwise rule from the source material: each pullback
        # bar's volume compared against the *specific* green candle
        # immediately preceding the pullback (the last up-move bar), not
        # just the aggregate totals above -- catches a single oversized red
        # bar within a multi-bar pullback that the aggregate check alone
        # can still let through.
        prior_green_bar = up_move_bars[-1]
        if any(b.volume >= prior_green_bar.volume for b in pullback_bars):
            return PullbackValidity(
                False, "pullback bar volume not lighter than the immediately preceding green candle"
            )

    if config.require_rising_volume_on_advance and not has_rising_volume_on_advance(up_move_bars):
        return PullbackValidity(False, "volume declining on the preceding advance")

    # Both level checks below are evaluated AT EACH PULLBACK BAR's own
    # moment, not against the current value. On a rising stock -- which
    # every candidate here is -- VWAP and the 9 EMA are still climbing
    # through the pullback, so comparing a bar that closed four minutes ago
    # against the value as of now is systematically too strict: it rejected
    # pullbacks that genuinely did hold the level when they printed. Where
    # the point-in-time value is unavailable (too few bars for the EMA, or a
    # caller that synthesised its own bars) the current value is used, which
    # is the previous behaviour.
    for offset, bar in enumerate(pullback_bars):
        index = None if pullback_start is None else pullback_start + offset

        bar_vwap = ctx.vwap_at(index) if index is not None else None
        if bar_vwap is None:
            bar_vwap = ctx.vwap
        if bar_vwap is not None and bar.low < bar_vwap:
            return PullbackValidity(False, "pullback broke below VWAP")

        bar_ema_9 = ctx.ema_9_at(index) if index is not None else None
        if bar_ema_9 is None:
            bar_ema_9 = ctx.ema_9
        # Close-based, not low-based (unlike the VWAP check above): the
        # source material explicitly tolerates "a brief single-candle wick
        # below the 9 EMA that immediately reclaims it" as noise, not a
        # disqualifying break -- only a bar that actually *closes* below
        # the EMA counts as a real break.
        if bar_ema_9 is not None and bar.close < bar_ema_9:
            return PullbackValidity(False, "pullback broke below 9 EMA")

    # (9, 20) matches Ross Cameron's actual chart MACD setup (computed from
    # his 9/20 EMA pair), not the textbook (12, 26) default -- confirmed by
    # a later, more execution-detailed transcript.
    macd_result = ctx.macd(fast=9, slow=20)
    if macd_result is not None:
        macd_line, signal_line = macd_result
        if macd_line <= signal_line:
            return PullbackValidity(False, "MACD not bullish")

    if config.reject_topping_tail and any(
        is_topping_tail(b, wick_ratio=config.topping_tail_wick_ratio) for b in pullback_bars
    ):
        return PullbackValidity(False, "topping tail in pullback")

    if config.reject_high_volume_red_bar and up_move_bars:
        avg_recent_volume = sum(b.volume for b in up_move_bars) / len(up_move_bars)
        if any(
            is_high_volume_red_bar(b, avg_recent_volume, multiple=config.high_volume_red_bar_multiple)
            for b in pullback_bars
        ):
            return PullbackValidity(False, "high-volume red bar in pullback")

    if config.require_5m_macd_confirmation or config.reject_5m_topping_tail:
        five_min_bars = resample_bars(ctx.bars, bucket_minutes=5)
        if config.require_5m_macd_confirmation:
            five_min_macd = macd(five_min_bars, fast=9, slow=20)
            if five_min_macd is not None:
                macd_line, signal_line = five_min_macd
                if macd_line <= signal_line:
                    return PullbackValidity(False, "5-minute MACD not bullish (multi-timeframe veto)")
        if config.reject_5m_topping_tail:
            # The last COMPLETE bucket -- see resample_bars' drop_partial.
            complete = resample_bars(ctx.bars, bucket_minutes=5, drop_partial=True)
            if complete and is_topping_tail(complete[-1], wick_ratio=config.topping_tail_wick_ratio):
                return PullbackValidity(False, "5-minute topping tail (multi-timeframe veto)")

    return PullbackValidity(True)
