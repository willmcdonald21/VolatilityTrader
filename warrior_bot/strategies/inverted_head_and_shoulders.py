from __future__ import annotations

from datetime import datetime

from warrior_bot.config import InvertedHeadAndShouldersConfig
from warrior_bot.signals.signal import Signal
from warrior_bot.strategies.base_strategy import BaseStrategy, SymbolContext
from warrior_bot.strategies.indicators import candle_strength, crossed_round_number
from warrior_bot.utils.time_utils import session_elapsed_fraction


class InvertedHeadAndShouldersStrategy(BaseStrategy):
    """Bullish reversal pattern: a low (left shoulder), a lower low (head),
    then a higher low roughly matching the left shoulder (right shoulder),
    with the "neckline" formed by the two reaction highs in between. Entry
    on the break of the neckline, stop below the right shoulder.

    Secondary pattern from the source material (named and identified in a
    live trade recap, not one of the four core taught setups) -- see
    docs/strategy_decisions.md. Only ever ingests 1-minute bars like the
    rest of this bot, so shoulder/head detection works off local extrema in
    that window, same idiom as bull_flag's spike/pullback and abcd's B/C
    detection (plain min/max over sub-windows, not the standalone
    swing_points() helper, to stay consistent with those two)."""

    name = "inverted_head_and_shoulders"
    config: InvertedHeadAndShouldersConfig
    LOOKBACK_BARS = 40

    def evaluate(self, ctx: SymbolContext, now: datetime) -> Signal | None:
        cfg = self.config
        state = self.state_for(ctx.symbol)
        if state.get("triggered"):
            return None
        if not self._check_engaged(ctx):
            return self._reject(ctx, "macd_bearish")

        rel_vol = ctx.relative_volume(session_elapsed_fraction(now))
        if rel_vol is None or rel_vol < cfg.min_rel_volume:
            return self._reject(ctx, "relative_volume")

        prior_bars = ctx.bars[:-1]
        # minimum viable window: 1 left-shoulder bar + 1 head bar + 1 right-shoulder bar
        if len(prior_bars) < 5:
            return None

        window = prior_bars[-self.LOOKBACK_BARS :]

        # Head = the single deepest trough in the window.
        head_idx = min(range(len(window)), key=lambda i: window[i].low)
        head_low = window[head_idx].low

        # Need real bars on both sides of the head to form two shoulders.
        if head_idx < 1 or head_idx > len(window) - 2:
            return self._reject(ctx, "head_at_window_edge")

        left_segment = window[:head_idx]
        right_segment = window[head_idx + 1 :]

        left_idx = min(range(len(left_segment)), key=lambda i: left_segment[i].low)
        left_shoulder_low = left_segment[left_idx].low

        right_idx = min(range(len(right_segment)), key=lambda i: right_segment[i].low)
        right_shoulder_low = right_segment[right_idx].low
        right_idx_in_window = head_idx + 1 + right_idx

        if not (head_low < left_shoulder_low and head_low < right_shoulder_low):
            return self._reject(ctx, "head_not_deepest")

        shoulder_spread_pct = abs(left_shoulder_low - right_shoulder_low) / head_low * 100.0 if head_low > 0 else 100.0
        if shoulder_spread_pct > cfg.max_shoulder_asymmetry_pct:
            return self._reject(ctx, "shoulder_asymmetry")

        # Neckline: the reaction high between left-shoulder-and-head, and
        # the reaction high between head-and-right-shoulder. Take the lower
        # of the two (conservative horizontal breakout level, rather than a
        # sloped line through both) -- same simplification the rest of this
        # bot uses elsewhere (opening_range/flag_high are horizontal levels
        # too, not slopes).
        neckline_left = max(b.high for b in window[left_idx : head_idx + 1])
        neckline_right = max(b.high for b in window[head_idx : right_idx_in_window + 1])
        neckline = min(neckline_left, neckline_right)
        if neckline <= 0:
            return self._reject(ctx, "invalid_neckline")

        head_depth_pct = (neckline - head_low) / neckline * 100.0
        if head_depth_pct < cfg.min_head_depth_pct:
            return self._reject(ctx, "head_depth")

        current_bar = ctx.bars[-1]
        if current_bar.close <= neckline:
            return self._reject(ctx, "no_breakout")

        if candle_strength(current_bar) < cfg.min_breakout_candle_strength:
            return self._reject(ctx, "weak_breakout_candle")

        entry_price = current_bar.close
        stop_price = right_shoulder_low * (1 - cfg.stop_buffer_pct / 100.0)
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
                "left_shoulder_low": left_shoulder_low,
                "head_low": head_low,
                "right_shoulder_low": right_shoulder_low,
                "neckline": neckline,
                "round_number_breakout": crossed_round_number(prior_bar.close, current_bar.close),
            },
        )
