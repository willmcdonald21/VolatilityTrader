from __future__ import annotations

from typing import Iterable

from warrior_bot.strategies.base_strategy import SymbolContext


def count_extreme_gainers(contexts: Iterable[SymbolContext], threshold_pct: float = 100.0) -> int:
    """Count of onboarded symbols currently gapped up at least
    `threshold_pct`% -- a market-wide "breadth of extreme movers" proxy for
    today's regime (source material: "on days with zero stocks up over
    100%... that alone is read as a signal the market is comparatively
    cold").

    Approximation, not a literal market-wide count: only symbols the
    scanner has actually surfaced and this bot has onboarded are visible
    here, bounded by `scanner.above_price`/`below_price`/`above_volume` --
    a higher-priced runner outside the configured price band would never
    appear even on a genuinely hot day. Useful as a rough, loggable signal
    for the maintainer to eyeball each morning; deliberately not wired into
    automatic position sizing, since that price-band bias could
    systematically undercount breadth and falsely trigger a "cold market"
    read on an otherwise strong day.
    """
    return sum(1 for ctx in contexts if ctx.gap_pct is not None and ctx.gap_pct >= threshold_pct)
