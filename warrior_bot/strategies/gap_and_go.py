from __future__ import annotations

from datetime import datetime

from warrior_bot.config import GapAndGoConfig, PullbackQualityConfig
from warrior_bot.scanner.float_provider import FloatProvider
from warrior_bot.signals.signal import Signal
from warrior_bot.strategies.base_strategy import BaseStrategy, SymbolContext
from warrior_bot.strategies.indicators import candle_strength, crossed_round_number, is_entry_too_extended
from warrior_bot.strategies.pullback_validity import validate_pullback
from warrior_bot.utils.time_utils import session_elapsed_fraction


class GapAndGoStrategy(BaseStrategy):
    """Classic Warrior-Trading gap-and-go: a low-priced, high-relative-volume
    gapper spikes, pulls back (ideally holding VWAP and the 9 EMA), then
    breaks the pullback's high on volume. Enter long on the breakout, stop
    below the pullback low.

    Reworked 2026-09-26 from a straight opening-range-breakout trigger (no
    pullback of any kind) to this spike -> consolidation -> breakout
    structure, mirroring bull_flag.py exactly -- Ross's own documented
    beginner rule for gap-and-go is "buy the first pullback", not "buy the
    initial breakout", and the old version had no pullback-detection logic
    at all. The gap/price/float/relative-volume qualification checks below
    are unchanged; only what counts as "the entry" has changed.
    """

    name = "gap_and_go"
    config: GapAndGoConfig
    LOOKBACK_BARS = 40

    def __init__(
        self,
        config: GapAndGoConfig,
        float_provider: FloatProvider | None = None,
        pullback_quality_config: PullbackQualityConfig | None = None,
    ):
        super().__init__(config)
        self.float_provider = float_provider
        self.pullback_quality_config = pullback_quality_config or PullbackQualityConfig()

    def evaluate(self, ctx: SymbolContext, now: datetime) -> Signal | None:
        cfg = self.config
        if len(ctx.bars) < 2:
            return None

        state = self.state_for(ctx.symbol)
        if self.already_triggered(ctx, now):
            return None
        if not self._check_engaged(ctx):
            return self._reject(ctx, "macd_bearish")

        price = ctx.last_price
        if price is None or not (cfg.min_price <= price <= cfg.max_price):
            return self._reject(ctx, "price_out_of_range")

        if cfg.enable_float_filter and self.float_provider is not None:
            if not self.float_provider.passes_filter(ctx.symbol, cfg.max_float_shares):
                return self._reject(ctx, "float_filter")

        if cfg.min_float_rotation > 0 and self.float_provider is not None:
            float_shares = self.float_provider.get_float_shares(ctx.symbol)
            if float_shares is not None and float_shares > 0:
                rotation = ctx.cumulative_volume / float_shares
                if rotation < cfg.min_float_rotation:
                    return self._reject(ctx, "float_rotation")

        gap = ctx.gap_pct
        if gap is None or gap < cfg.min_gap_pct:
            return self._reject(ctx, "gap_pct")
        if cfg.max_gap_pct is not None and gap > cfg.max_gap_pct:
            return self._reject(ctx, "gap_too_large")

        rel_vol = ctx.relative_volume(session_elapsed_fraction(now))
        if rel_vol is None or rel_vol < cfg.min_rel_volume:
            return self._reject(ctx, "relative_volume")
        if cfg.max_rel_volume is not None and rel_vol > cfg.max_rel_volume:
            return self._reject(ctx, "relative_volume_too_high")

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
            return self._reject(ctx, "invalid_baseline")
        spike_pct = (spike_high - baseline_low) / baseline_low * 100.0
        if spike_pct < cfg.min_spike_pct:
            return self._reject(ctx, "spike_pct")

        consolidation = window[spike_idx + 1 :]
        if not (cfg.min_consolidation_bars <= len(consolidation) <= cfg.max_consolidation_bars):
            return self._reject(ctx, "consolidation_bars")

        pullback_low_bar = min(consolidation, key=lambda b: b.low)
        pullback_low = pullback_low_bar.low
        spike_range = spike_high - baseline_low
        pullback_pct = (spike_high - pullback_low) / spike_range * 100.0 if spike_range > 0 else 100.0
        if pullback_pct > cfg.max_pullback_pct:
            return self._reject(ctx, "pullback_pct")

        # Ross's beginner gap-and-go entry rule specifically requires the
        # pullback to hold VWAP and the 9 EMA -- validate_pullback's base
        # checks already enforce exactly that (plus the same volume-profile/
        # topping-tail/MACD quality gates bull_flag/abcd already share), so
        # this is what actually closes that fidelity gap, not a bespoke
        # VWAP-distance check.
        validity = validate_pullback(
            pullback_bars=consolidation, up_move_bars=pre_spike, ctx=ctx, config=self.pullback_quality_config
        )
        if not validity.valid:
            return self._reject(ctx, f"pullback_quality:{validity.reason}")

        breakout_high = max(b.high for b in consolidation)
        current_bar = ctx.bars[-1]
        if current_bar.close <= breakout_high:
            return self._reject(ctx, "no_breakout")

        if candle_strength(current_bar) < cfg.min_breakout_candle_strength:
            return self._reject(ctx, "weak_breakout_candle")

        if is_entry_too_extended(
            current_bar, breakout_high, ctx.atr(), cfg.max_extension_atr_multiple, cfg.max_extension_pct
        ):
            return self._reject(ctx, "breakout_too_extended")

        entry_price = current_bar.close
        stop_price = pullback_low * (1 - cfg.stop_buffer_pct / 100.0)
        if stop_price >= entry_price:
            return self._reject(ctx, "invalid_stop")

        prior_bar = ctx.bars[-2]
        state["triggered"] = True
        return self._build_signal(
            ctx,
            now,
            entry_price=entry_price,
            stop_price=stop_price,
            target_r_multiple=cfg.target_r_multiple,
            context={
                "gap_pct": gap,
                "relative_volume": rel_vol,
                "spike_high": spike_high,
                "breakout_high": breakout_high,
                "pullback_pct": pullback_pct,
                "round_number_breakout": crossed_round_number(prior_bar.close, current_bar.close),
            },
        )
