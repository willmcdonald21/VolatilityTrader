from __future__ import annotations

from datetime import datetime

from warrior_bot.config import BullFlagConfig, PullbackQualityConfig
from warrior_bot.signals.signal import Signal
from warrior_bot.strategies.base_strategy import BaseStrategy, SymbolContext
from warrior_bot.strategies.indicators import candle_strength, crossed_round_number, is_bottoming_tail
from warrior_bot.strategies.pullback_validity import validate_pullback
from warrior_bot.utils.time_utils import session_elapsed_fraction


class BullFlagStrategy(BaseStrategy):
    """Momentum spike followed by a tight pullback/consolidation (the
    "flag"), entered on the break of the flag's high. Stop below the flag
    low.

    Per the source material, "micro pullback" and "bull flag" name the
    *same* pattern -- only the chart timeframe differs (micro pullback on
    10-second/1-minute bars, bull flag on 5-minute+ bars). This bot only
    ever ingests 1-minute bars, so every signal this class produces is,
    strictly speaking, a micro pullback; the "bull_flag" name is kept for
    continuity with the rest of the codebase/config, not as a claim about
    timeframe. Running this same detection logic against 5-minute-resampled
    bars (a genuine slower-timeframe "bull flag" pass) is a deferred,
    separate piece of work -- see docs/strategy_decisions.md.
    """

    name = "bull_flag"
    config: BullFlagConfig
    LOOKBACK_BARS = 40

    def __init__(self, config: BullFlagConfig, pullback_quality_config: PullbackQualityConfig | None = None):
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
        # minimum viable window: 1 baseline bar + 1 spike bar + the shortest allowed consolidation
        if len(prior_bars) < cfg.min_consolidation_bars + 2:
            return None

        window = prior_bars[-self.LOOKBACK_BARS :]
        spike_idx = max(range(len(window)), key=lambda i: window[i].high)
        spike_high = window[spike_idx].high

        pre_spike = window[: spike_idx + 1]
        baseline_low = min(b.low for b in pre_spike)
        if baseline_low <= 0:
            return None
        spike_pct = (spike_high - baseline_low) / baseline_low * 100.0
        if spike_pct < cfg.min_spike_pct:
            return None

        consolidation = window[spike_idx + 1 :]
        if not (cfg.min_consolidation_bars <= len(consolidation) <= cfg.max_consolidation_bars):
            return None

        pullback_low_bar = min(consolidation, key=lambda b: b.low)
        pullback_low = pullback_low_bar.low
        spike_range = spike_high - baseline_low
        pullback_pct = (spike_high - pullback_low) / spike_range * 100.0 if spike_range > 0 else 100.0
        if pullback_pct > cfg.max_pullback_pct:
            return None

        validity = validate_pullback(
            pullback_bars=consolidation, up_move_bars=pre_spike, ctx=ctx, config=self.pullback_quality_config
        )
        if not validity.valid:
            return None

        flag_high = max(b.high for b in consolidation)
        current_bar = ctx.bars[-1]
        if current_bar.close <= flag_high:
            return None

        if candle_strength(current_bar) < cfg.min_breakout_candle_strength:
            return None

        entry_price = current_bar.close
        stop_price = pullback_low * (1 - cfg.stop_buffer_pct / 100.0)
        if stop_price >= entry_price:
            return None

        prior_bar = ctx.bars[-2]
        state["triggered"] = True
        return self._build_signal(
            ctx,
            now,
            entry_price=entry_price,
            stop_price=stop_price,
            target_r_multiple=cfg.target_r_multiple,
            context={
                "spike_high": spike_high,
                "flag_high": flag_high,
                "pullback_pct": pullback_pct,
                "bottoming_tail_confirmation": is_bottoming_tail(pullback_low_bar),
                "round_number_breakout": crossed_round_number(prior_bar.close, current_bar.close),
            },
        )
