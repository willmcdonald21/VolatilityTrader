from __future__ import annotations

from datetime import datetime

from warrior_bot.config import GapAndGoConfig
from warrior_bot.scanner.float_provider import FloatProvider
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

    def __init__(self, config: GapAndGoConfig, float_provider: FloatProvider | None = None):
        super().__init__(config)
        self.float_provider = float_provider

    def evaluate(self, ctx: SymbolContext, now: datetime) -> Signal | None:
        cfg = self.config
        if len(ctx.bars) < 2:
            return None

        state = self.state_for(ctx.symbol)
        if state.get("triggered"):
            return None
        if not self._check_engaged(ctx):
            return None

        price = ctx.last_price
        if price is None or not (cfg.min_price <= price <= cfg.max_price):
            return None

        if cfg.enable_float_filter and self.float_provider is not None:
            if not self.float_provider.passes_filter(ctx.symbol, cfg.max_float_shares):
                return None

        if cfg.min_float_rotation > 0 and self.float_provider is not None:
            float_shares = self.float_provider.get_float_shares(ctx.symbol)
            if float_shares is not None and float_shares > 0:
                rotation = ctx.cumulative_volume / float_shares
                if rotation < cfg.min_float_rotation:
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
