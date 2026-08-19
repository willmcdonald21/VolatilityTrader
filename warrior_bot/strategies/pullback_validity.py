from __future__ import annotations

from dataclasses import dataclass

from warrior_bot.config import PullbackQualityConfig
from warrior_bot.strategies.base_strategy import SymbolContext
from warrior_bot.strategies.indicators import (
    Bar,
    is_high_volume_red_bar,
    is_topping_tail,
    macd,
    resample_bars,
)


@dataclass(frozen=True)
class PullbackValidity:
    valid: bool
    reason: str | None = None


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
    lighter volume on the pullback than the preceding up-move, holds above
    VWAP, holds above the 9 EMA, and a non-bearish MACD. A gate is only
    ever enforced when its underlying data is actually available --
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

    pullback_low = min(b.low for b in pullback_bars)

    vwap = ctx.vwap
    if vwap is not None and pullback_low < vwap:
        return PullbackValidity(False, "pullback broke below VWAP")

    ema_9 = ctx.ema_9
    if ema_9 is not None and pullback_low < ema_9:
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
        if config.reject_5m_topping_tail and five_min_bars:
            if is_topping_tail(five_min_bars[-1], wick_ratio=config.topping_tail_wick_ratio):
                return PullbackValidity(False, "5-minute topping tail (multi-timeframe veto)")

    return PullbackValidity(True)
