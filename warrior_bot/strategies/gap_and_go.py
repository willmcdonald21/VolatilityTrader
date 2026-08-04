from __future__ import annotations

from datetime import datetime

from warrior_bot.config import GapAndGoConfig
from warrior_bot.signals.signal import Signal
from warrior_bot.strategies.base_strategy import BaseStrategy, SymbolContext
from warrior_bot.strategies.indicators import opening_range
from warrior_bot.utils.time_utils import session_elapsed_fraction


class GapAndGoStrategy(BaseStrategy):
    """Classic Warrior-Trading gap-and-go: a low-priced, high-relative-volume
    gapper breaks above its pre-market/opening-range high on volume. Enter
    long on the breakout, stop just below the breakout level."""

    name = "gap_and_go"
    config: GapAndGoConfig

    def evaluate(self, ctx: SymbolContext, now: datetime) -> Signal | None:
        cfg = self.config
        if len(ctx.bars) < 2:
            return None

        state = self.state_for(ctx.symbol)
        if state.get("triggered"):
            return None

        price = ctx.last_price
        if price is None or not (cfg.min_price <= price <= cfg.max_price):
            return None

        gap = ctx.gap_pct
        if gap is None or gap < cfg.min_gap_pct:
            return None

        rel_vol = ctx.relative_volume(session_elapsed_fraction(now))
        if rel_vol is None or rel_vol < cfg.min_rel_volume:
            return None

        # Breakout level is computed from bars *before* the current one, so
        # the current bar is judged against a level it couldn't itself set.
        prior_bars = ctx.bars[:-1]
        range_ = opening_range(prior_bars, cfg.breakout_lookback_bars)
        if range_ is None:
            return None
        breakout_high, _ = range_

        current_bar = ctx.bars[-1]
        if current_bar.close <= breakout_high:
            return None

        entry_price = current_bar.close
        stop_price = breakout_high * (1 - cfg.stop_buffer_pct / 100.0)
        if stop_price >= entry_price:
            return None

        state["triggered"] = True
        return self._build_signal(
            ctx,
            now,
            entry_price=entry_price,
            stop_price=stop_price,
            target_r_multiple=cfg.target_r_multiple,
            context={"gap_pct": gap, "relative_volume": rel_vol, "breakout_high": breakout_high},
        )
