from __future__ import annotations

from dataclasses import dataclass

from warrior_bot.strategies.base_strategy import SymbolContext
from warrior_bot.strategies.indicators import Bar


@dataclass(frozen=True)
class PullbackValidity:
    valid: bool
    reason: str | None = None


def validate_pullback(pullback_bars: list[Bar], up_move_bars: list[Bar], ctx: SymbolContext) -> PullbackValidity:
    """Shared pullback-quality gate for bull_flag and abcd -- both are
    "spike then pullback then breakout" patterns, and the source material's
    validity rules are about pullback quality generically, not specific to
    either strategy's own consolidation-depth logic.

    Four checks, all framed as hard invalidations in the source material
    ("I would not take that trade" / "do not take the breakout entry"):
    lighter volume on the pullback than the preceding up-move, holds above
    VWAP, holds above the 9 EMA, and a non-bearish MACD. A gate is only
    ever enforced when its underlying data is actually available --
    insufficient warm-up history (e.g. MACD needs ~34 bars) means
    "unknown", not "invalid", so it never blocks a signal on its own.
    """
    if not pullback_bars or not up_move_bars:
        return PullbackValidity(True)

    pullback_volume = sum(b.volume for b in pullback_bars)
    up_move_volume = sum(b.volume for b in up_move_bars)
    if up_move_volume > 0 and pullback_volume >= up_move_volume:
        return PullbackValidity(False, "pullback volume not lighter than the preceding up-move")

    pullback_low = min(b.low for b in pullback_bars)

    vwap = ctx.vwap
    if vwap is not None and pullback_low < vwap:
        return PullbackValidity(False, "pullback broke below VWAP")

    ema_9 = ctx.ema_9
    if ema_9 is not None and pullback_low < ema_9:
        return PullbackValidity(False, "pullback broke below 9 EMA")

    macd_result = ctx.macd()
    if macd_result is not None:
        macd_line, signal_line = macd_result
        if macd_line <= signal_line:
            return PullbackValidity(False, "MACD not bullish")

    return PullbackValidity(True)
