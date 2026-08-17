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
        cfg = self.config
        state = self.state_for(ctx.symbol)
        if state.get("triggered"):
            return None
        if not self._check_engaged(ctx):
            return None
        if len(ctx.bars) < 3:
            return None

        vwap_price = ctx.vwap
        current_bar = ctx.bars[-1]
        prev_bar = ctx.bars[-2]

        if ctx.prior_close is not None and is_red_to_green(ctx.prior_close, current_bar.close, prev_bar.close):
            rel_vol = ctx.relative_volume(session_elapsed_fraction(now))
            if rel_vol is not None and rel_vol >= cfg.red_to_green_volume_multiple:
                entry_price = current_bar.close
                lookback_low = min(b.low for b in ctx.bars[-3:])
                stop_price = lookback_low * (1 - cfg.stop_buffer_pct / 100.0)
                if stop_price < entry_price:
                    state["triggered"] = True
                    return self._build_signal(
                        ctx,
                        now,
                        entry_price=entry_price,
                        stop_price=stop_price,
                        target_r_multiple=cfg.target_r_multiple,
                        context={"setup": "red_to_green", "relative_volume": rel_vol},
                    )

        if vwap_price is not None and vwap_price > 0:
            distance_pct = abs(prev_bar.low - vwap_price) / vwap_price * 100.0
            touched_vwap = distance_pct <= cfg.max_vwap_distance_pct
            bounced = current_bar.close > prev_bar.high and current_bar.close > vwap_price
            if touched_vwap and bounced:
                rel_vol = ctx.relative_volume(session_elapsed_fraction(now))
                if rel_vol is not None and rel_vol >= cfg.min_rel_volume:
                    entry_price = current_bar.close
                    stop_price = min(prev_bar.low, current_bar.low) * (1 - cfg.stop_buffer_pct / 100.0)
                    if stop_price < entry_price:
                        state["triggered"] = True
                        return self._build_signal(
                            ctx,
                            now,
                            entry_price=entry_price,
                            stop_price=stop_price,
                            target_r_multiple=cfg.target_r_multiple,
                            context={"setup": "vwap_bounce", "vwap": vwap_price, "relative_volume": rel_vol},
                        )

        return None
