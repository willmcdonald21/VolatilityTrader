from __future__ import annotations

from datetime import datetime

from warrior_bot.config import AbcdConfig, PullbackQualityConfig
from warrior_bot.signals.signal import Signal
from warrior_bot.strategies.base_strategy import BaseStrategy, SymbolContext
from warrior_bot.strategies.indicators import candle_strength, is_bottoming_tail
from warrior_bot.strategies.pullback_validity import validate_pullback
from warrior_bot.utils.time_utils import session_elapsed_fraction


class AbcdStrategy(BaseStrategy):
    """Warrior Trading ABCD: A = baseline before the move, B = first spike
    high, C = pullback low after B, D = breakout back above B on the second
    leg. Entry at the D breakout, stop below C.

    Distinguished from bull_flag by pullback depth (a real, deeper pullback
    bounded by min/max %) rather than a tight multi-bar consolidation."""

    name = "abcd"
    config: AbcdConfig
    LOOKBACK_BARS = 40

    def __init__(self, config: AbcdConfig, pullback_quality_config: PullbackQualityConfig | None = None):
        super().__init__(config)
        self.pullback_quality_config = pullback_quality_config or PullbackQualityConfig()

    def evaluate(self, ctx: SymbolContext, now: datetime) -> Signal | None:
        cfg = self.config
        state = self.state_for(ctx.symbol)
        if state.get("triggered"):
            return None
        if not self._check_engaged(ctx):
            return None

        rel_vol = ctx.relative_volume(session_elapsed_fraction(now))
        if rel_vol is None or rel_vol < cfg.min_rel_volume:
            return None

        prior_bars = ctx.bars[:-1]
        # minimum viable window: 1 baseline (A) bar + 1 spike (B) bar + at least 1 pullback (C) bar
        if len(prior_bars) < 3:
            return None

        window = prior_bars[-self.LOOKBACK_BARS :]
        b_idx = max(range(len(window)), key=lambda i: window[i].high)
        b_high = window[b_idx].high

        pre_b = window[: b_idx + 1]
        a_low = min(b.low for b in pre_b)
        if a_low <= 0:
            return None
        ab_move_pct = (b_high - a_low) / a_low * 100.0
        if ab_move_pct < cfg.min_ab_move_pct:
            return None

        post_b = window[b_idx + 1 :]
        if not post_b:
            return None
        c_low_bar = min(post_b, key=lambda b: b.low)
        c_low = c_low_bar.low
        ab_range = b_high - a_low
        bc_pullback_pct = (b_high - c_low) / ab_range * 100.0 if ab_range > 0 else 100.0
        if not (cfg.min_bc_pullback_pct <= bc_pullback_pct <= cfg.max_bc_pullback_pct):
            return None

        validity = validate_pullback(
            pullback_bars=post_b, up_move_bars=pre_b, ctx=ctx, config=self.pullback_quality_config
        )
        if not validity.valid:
            return None

        current_bar = ctx.bars[-1]
        if current_bar.close <= b_high:
            return None

        if candle_strength(current_bar) < cfg.min_breakout_candle_strength:
            return None

        entry_price = current_bar.close
        stop_price = c_low * (1 - cfg.stop_buffer_pct / 100.0)
        if stop_price >= entry_price:
            return None

        state["triggered"] = True
        return self._build_signal(
            ctx,
            now,
            entry_price=entry_price,
            stop_price=stop_price,
            target_r_multiple=cfg.target_r_multiple,
            context={
                "a_low": a_low,
                "b_high": b_high,
                "c_low": c_low,
                "bc_pullback_pct": bc_pullback_pct,
                "bottoming_tail_confirmation": is_bottoming_tail(c_low_bar),
            },
        )
