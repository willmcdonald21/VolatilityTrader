from __future__ import annotations

from datetime import datetime

from warrior_bot.config import BullFlagConfig
from warrior_bot.signals.signal import Signal
from warrior_bot.strategies.base_strategy import BaseStrategy, SymbolContext
from warrior_bot.strategies.pullback_validity import validate_pullback


class BullFlagStrategy(BaseStrategy):
    """Momentum spike followed by a tight pullback/consolidation (the
    "flag"), entered on the break of the flag's high. Stop below the flag
    low."""

    name = "bull_flag"
    config: BullFlagConfig
    LOOKBACK_BARS = 40

    def evaluate(self, ctx: SymbolContext, now: datetime) -> Signal | None:
        cfg = self.config
        state = self.state_for(ctx.symbol)
        if state.get("triggered"):
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

        pullback_low = min(b.low for b in consolidation)
        spike_range = spike_high - baseline_low
        pullback_pct = (spike_high - pullback_low) / spike_range * 100.0 if spike_range > 0 else 100.0
        if pullback_pct > cfg.max_pullback_pct:
            return None

        validity = validate_pullback(pullback_bars=consolidation, up_move_bars=pre_spike, ctx=ctx)
        if not validity.valid:
            return None

        flag_high = max(b.high for b in consolidation)
        current_bar = ctx.bars[-1]
        if current_bar.close <= flag_high:
            return None

        entry_price = current_bar.close
        stop_price = pullback_low * (1 - cfg.stop_buffer_pct / 100.0)
        if stop_price >= entry_price:
            return None

        state["triggered"] = True
        return self._build_signal(
            ctx,
            now,
            entry_price=entry_price,
            stop_price=stop_price,
            target_r_multiple=cfg.target_r_multiple,
            context={"spike_high": spike_high, "flag_high": flag_high, "pullback_pct": pullback_pct},
        )
