from __future__ import annotations

from datetime import datetime

from warrior_bot.config import VwapReversionConfig
from warrior_bot.signals.signal import Signal
from warrior_bot.strategies.base_strategy import BaseStrategy, SymbolContext
from warrior_bot.strategies.indicators import is_red_to_green
from warrior_bot.utils.time_utils import session_elapsed_fraction


class VwapReversionStrategy(BaseStrategy):
    """Two related mean-reversion setups sharing VWAP context:

    1. VWAP bounce: price pulls back to touch VWAP, then reclaims the prior
       bar's high and closes back above VWAP — entered on the bounce bar.
    2. Red-to-green: price crosses from below to at/above the prior day's
       close, confirmed by elevated relative volume.
    """

    name = "vwap_reversion"
    config: VwapReversionConfig

    def evaluate(self, ctx: SymbolContext, now: datetime) -> Signal | None:
        state = self.state_for(ctx.symbol)
        if state.get("triggered"):
            return None
        if not self._check_engaged(ctx):
            return self._reject(ctx, "macd_bearish")
        if len(ctx.bars) < 3:
            return None

        signal = self._check_red_to_green(ctx, now, state)
        if signal is not None:
            return signal
        return self._check_vwap_bounce(ctx, now, state)

    def _check_red_to_green(self, ctx: SymbolContext, now: datetime, state: dict) -> Signal | None:
        cfg = self.config
        current_bar = ctx.bars[-1]
        prev_bar = ctx.bars[-2]

        if ctx.prior_close is None or not is_red_to_green(ctx.prior_close, current_bar.close, prev_bar.close):
            return None

        rel_vol = ctx.relative_volume(session_elapsed_fraction(now))
        if rel_vol is None or rel_vol < cfg.red_to_green_volume_multiple:
            return self._reject(ctx, "red_to_green_relative_volume")

        entry_price = current_bar.close
        lookback_low = min(b.low for b in ctx.bars[-3:])
        stop_price = lookback_low * (1 - cfg.stop_buffer_pct / 100.0)
        if stop_price >= entry_price:
            return self._reject(ctx, "red_to_green_invalid_stop")

        state["triggered"] = True
        return self._build_signal(
            ctx,
            now,
            entry_price=entry_price,
            stop_price=stop_price,
            target_r_multiple=cfg.target_r_multiple,
            context={"setup": "red_to_green", "relative_volume": rel_vol},
        )

    def _check_vwap_bounce(self, ctx: SymbolContext, now: datetime, state: dict) -> Signal | None:
        cfg = self.config
        vwap_price = ctx.vwap
        current_bar = ctx.bars[-1]
        prev_bar = ctx.bars[-2]

        if vwap_price is None or vwap_price <= 0:
            return self._reject(ctx, "vwap_unavailable")

        distance_pct = abs(prev_bar.low - vwap_price) / vwap_price * 100.0
        if distance_pct > cfg.max_vwap_distance_pct:
            return self._reject(ctx, "vwap_distance")

        if not (current_bar.close > prev_bar.high and current_bar.close > vwap_price):
            return self._reject(ctx, "vwap_no_bounce")

        rel_vol = ctx.relative_volume(session_elapsed_fraction(now))
        if rel_vol is None or rel_vol < cfg.min_rel_volume:
            return self._reject(ctx, "vwap_bounce_relative_volume")

        entry_price = current_bar.close
        stop_price = min(prev_bar.low, current_bar.low) * (1 - cfg.stop_buffer_pct / 100.0)
        if stop_price >= entry_price:
            return self._reject(ctx, "vwap_bounce_invalid_stop")

        state["triggered"] = True
        return self._build_signal(
            ctx,
            now,
            entry_price=entry_price,
            stop_price=stop_price,
            target_r_multiple=cfg.target_r_multiple,
            context={"setup": "vwap_bounce", "vwap": vwap_price, "relative_volume": rel_vol},
        )
