from __future__ import annotations

import logging
from dataclasses import dataclass

from ib_async import IB, Contract, MarketOrder, Order, StopLimitOrder, StopOrder, Trade

from warrior_bot.config import ExitsConfig
from warrior_bot.persistence.journal import Journal
from warrior_bot.signals.signal import Signal
from warrior_bot.strategies.base_strategy import SymbolContext
from warrior_bot.strategies.indicators import (
    is_high_volume_red_bar,
    is_lower_low,
    is_momentum_exhausted,
    is_red_after_green,
    is_topping_tail,
    trailing_candidate,
)
from warrior_bot.utils.rounding import round_to_tick

logger = logging.getLogger("warrior_bot.execution.position_manager")


@dataclass
class ManagedPosition:
    symbol: str
    contract: Contract
    signal: Signal
    signal_id: int
    remaining_qty: int
    stop_order: Order
    stop_row_id: int
    # Authoritative current stop price, tracked here rather than read back
    # off stop_order.auxPrice -- ib_async's own openOrder callback
    # overwrites that attribute in place on any IBKR broadcast, so it can't
    # be trusted as "the price we last set."
    current_stop_price: float
    target_orders: list[Order]
    target_roles: list[str]  # parallel to target_orders
    breakeven_done: bool = False
    trailing_active: bool = False


class PositionManager:
    """Reacts to bar updates and fill events on already-submitted brackets
    to apply breakeven, trailing-stop, and multi-tier profit-taking
    management. `OrderManager` only places orders and journals fills/status
    — it never reacts to them, so this is the one place post-entry
    management lives.

    A symbol can hold up to two concurrently managed lots (a first entry
    plus one pyramid add-on, per RiskManager's sizing rule) -- each lot is
    tracked and managed independently, with its own stop/breakeven/trailing
    state, keyed by symbol as a list rather than a single position.

    Attaches its own `fillEvent` listeners onto the same `Trade` objects
    `OrderManager._attach_tracking` already wired for journaling — eventkit
    `Event`s support multiple independent listeners, so this is additive
    and doesn't disturb existing journaling behavior.
    """

    def __init__(self, ib: IB, journal: Journal, config: ExitsConfig, stop_limit_offset_pct: float = 0.5):
        self.ib = ib
        self.journal = journal
        self.config = config
        self.stop_limit_offset_pct = stop_limit_offset_pct
        self._positions: dict[str, list[ManagedPosition]] = {}

    def open_lot_count(self, symbol: str) -> int:
        return len(self._positions.get(symbol, []))

    def track(
        self,
        contract: Contract,
        signal: Signal,
        signal_id: int,
        stop_trade: Trade,
        stop_row_id: int,
        target_trades: list[Trade],
        target_roles: list[str],
    ) -> None:
        pos = ManagedPosition(
            symbol=signal.symbol,
            contract=contract,
            signal=signal,
            signal_id=signal_id,
            remaining_qty=int(stop_trade.order.totalQuantity),
            stop_order=stop_trade.order,
            stop_row_id=stop_row_id,
            current_stop_price=signal.stop_price,
            target_orders=[t.order for t in target_trades],
            target_roles=list(target_roles),
        )
        self._positions.setdefault(signal.symbol, []).append(pos)

        def make_on_target_fill(p: ManagedPosition):
            return lambda t, fill: self._on_target_fill(p, fill)

        for target_trade in target_trades:
            target_trade.fillEvent += make_on_target_fill(pos)
        self._wire_stop_fill(pos, stop_trade)

    def _wire_stop_fill(self, pos: ManagedPosition, trade: Trade) -> None:
        """Separated from track() so a replacement stop order (see
        _replace_stop_order) can re-wire the same fill handling onto its
        own fresh Trade object."""
        trade.fillEvent += lambda t, fill: self._on_stop_fill(pos, fill)

    def on_bar(self, ctx: SymbolContext) -> None:
        lots = self._positions.get(ctx.symbol)
        if not lots:
            return
        last_price = ctx.last_price
        if last_price is None:
            return

        for pos in list(lots):  # copy -- a reversal exit mutates the list mid-loop
            if self.config.reversal_exit.enabled and self._check_reversal_exit(pos, ctx):
                continue  # this lot is now flat -- evaluate any remaining lot for this symbol

            if not pos.breakeven_done and self.config.breakeven.enabled:
                self._check_breakeven(pos, last_price)

            if pos.breakeven_done and self.config.trailing.enabled:
                self._check_trailing(pos, ctx, last_price)

    def clear(self) -> None:
        """Drops all tracked positions with no IBKR side effects — used
        after a kill-switch/auto-flatten pass that already cancelled and
        flattened everything directly."""
        self._positions.clear()

    def _untrack(self, pos: ManagedPosition) -> None:
        lots = self._positions.get(pos.symbol)
        if lots is None:
            return
        lots[:] = [p for p in lots if p is not pos]
        if not lots:
            self._positions.pop(pos.symbol, None)

    # -- internal --

    def _current_r(self, pos: ManagedPosition, last_price: float) -> float:
        risk_per_share = pos.signal.risk_per_share
        if risk_per_share <= 0:
            return 0.0
        return (last_price - pos.signal.entry_price) / risk_per_share

    def _check_breakeven(self, pos: ManagedPosition, last_price: float) -> None:
        if self._current_r(pos, last_price) < self.config.breakeven.trigger_r_multiple:
            return
        entry = pos.signal.entry_price
        if entry > pos.current_stop_price:
            self._replace_stop_order(pos, entry)
            logger.info("Breakeven: moved stop for %s to entry %.4f", pos.symbol, entry)
        pos.breakeven_done = True

    def _check_trailing(self, pos: ManagedPosition, ctx: SymbolContext, last_price: float) -> None:
        candidate = trailing_candidate(
            last_price=last_price,
            ema_9=ctx.ema_9,
            atr=ctx.atr(self.config.trailing.atr_period),
            method=self.config.trailing.method,
            atr_multiple=self.config.trailing.atr_multiple,
        )
        if candidate is None:
            return
        current_stop = pos.current_stop_price
        new_stop = max(current_stop, candidate)
        if new_stop <= current_stop or new_stop >= last_price:
            # not tighter, or would be marketable/trigger immediately -- skip
            return

        just_activated = not pos.trailing_active
        pos.trailing_active = True
        # Only the single-full-quantity fallback ("target", no configured
        # profit tiers) gets cancelled on trailing activation. Configured
        # profit tiers are independent partial exits the user explicitly
        # wants taken at their own R-multiples -- trailing manages the stop
        # on whatever quantity is left, it doesn't preempt still-resting
        # tiers.
        if just_activated and pos.target_roles == ["target"] and pos.target_orders:
            self.ib.cancelOrder(pos.target_orders[0])
            pos.target_orders = []
            logger.info("Trailing activated for %s: cancelled static target", pos.symbol)

        self._replace_stop_order(pos, new_stop)
        logger.info("Trailing: moved stop for %s to %.4f", pos.symbol, new_stop)

    def _check_reversal_exit(self, pos: ManagedPosition, ctx: SymbolContext) -> bool:
        """Exit indicators from the source material that are directly
        OHLCV-computable (the others -- a large L2 seller, a hidden/iceberg
        seller, decelerating tape -- need order-book/tick data this bot
        doesn't have). Any one firing exits the remaining position
        immediately via a market order, matching the source material's
        "don't wait for the stop" urgency -- this is intentionally the
        second place in the bot (besides the kill switch) that sends a
        naked market order.

        "First lower low" only arms once breakeven has triggered -- the
        source material is explicit this confirmation is "evaluated only
        after the position is already profitable/trend-established", not
        from the first bar after entry.

        "Momentum exhaustion" (sequential shrinking green-candle bodies
        *and* shrinking volume) is unconditional like the other checks --
        it's a distinct warning sign that doesn't require any single bar
        to look bearish, so it isn't gated behind breakeven the way
        "first lower low" is."""
        cfg = self.config.reversal_exit
        bars = ctx.bars
        if len(bars) < 2:
            return False

        current_bar = bars[-1]
        prior_bar = bars[-2]
        reasons = []

        if is_topping_tail(current_bar, cfg.topping_tail_wick_ratio):
            reasons.append("topping_tail")
        if is_red_after_green(prior_bar, current_bar):
            reasons.append("red_after_green")
        if pos.breakeven_done and is_lower_low(current_bar, prior_bar):
            reasons.append("lower_low")
        recent = bars[-(cfg.volume_lookback_bars + 1) : -1]
        if recent:
            avg_recent_volume = sum(b.volume for b in recent) / len(recent)
            if is_high_volume_red_bar(current_bar, avg_recent_volume, cfg.volume_burst_multiple):
                reasons.append("volume_burst")
        if is_momentum_exhausted(bars, cfg.momentum_exhaustion_lookback_bars):
            reasons.append("momentum_exhaustion")

        if not reasons:
            return False

        self._reversal_exit(pos, reasons)
        return True

    def _reversal_exit(self, pos: ManagedPosition, reasons: list[str]) -> None:
        self.ib.cancelOrder(pos.stop_order)
        for target_order in pos.target_orders:
            self.ib.cancelOrder(target_order)
        order = MarketOrder("SELL", pos.remaining_qty)
        self.ib.placeOrder(pos.contract, order)
        reason_str = ",".join(reasons)
        logger.warning("Reversal exit for %s: %s (qty=%d)", pos.symbol, reason_str, pos.remaining_qty)
        self.journal.record_kill_switch_event(
            triggered_by=f"reversal_exit:{pos.symbol}:{reason_str}", action_taken="market_exit_position"
        )
        self._untrack(pos)

    def _replace_stop_order(self, pos: ManagedPosition, new_price: float) -> None:
        """Cancel-and-replace instead of in-place modification. IBKR
        rejects in-place modification of an order that's OCA-grouped or
        already been (partially) filled with error 10326 ("OCA group
        revision is not allowed") -- and ib_async marks the trade locally
        Cancelled on that error even though it may still be live at the
        broker, silently killing protection on the position. A fresh order
        with a fresh orderId sidesteps this entirely."""
        old_order = pos.stop_order
        new_price = round_to_tick(new_price)
        limit_price = None
        if getattr(old_order, "orderType", None) == "STP LMT":
            # keep the limit offset in the same direction bracket_builder
            # used when the order was first built, so trailing/breakeven
            # moves don't drift the limit's protective distance
            if old_order.action == "SELL":
                limit_price = round_to_tick(new_price * (1 - self.stop_limit_offset_pct / 100.0))
            else:
                limit_price = round_to_tick(new_price * (1 + self.stop_limit_offset_pct / 100.0))
            new_order = StopLimitOrder(
                old_order.action,
                old_order.totalQuantity,
                lmtPrice=limit_price,
                stopPrice=new_price,
                orderId=self.ib.client.getReqId(),
                transmit=True,
                outsideRth=True,
                tif="DAY",
            )
        else:
            new_order = StopOrder(
                old_order.action,
                old_order.totalQuantity,
                new_price,
                orderId=self.ib.client.getReqId(),
                transmit=True,
                outsideRth=True,
                tif="DAY",
            )

        # Re-link OCA only for the single-fallback-target case where the
        # stop was originally OCA'd with a still-resting target -- the
        # tiered profit-taking case never OCA-links the stop to begin with
        # (see build_bracket's docstring).
        if pos.target_roles == ["target"] and pos.target_orders and pos.target_orders[0].ocaGroup:
            IB.oneCancelsAll([new_order], pos.target_orders[0].ocaGroup, ocaType=1)

        self.ib.cancelOrder(old_order)
        new_trade = self.ib.placeOrder(pos.contract, new_order)

        new_row_id = self.journal.record_order(
            signal_id=pos.signal_id,
            ib_order_id=new_order.orderId,
            role="stop",
            action=new_order.action,
            qty=new_order.totalQuantity,
            order_type=new_order.orderType,
            limit_price=limit_price,
            stop_price=new_price,
            oca_group=new_order.ocaGroup or None,
            status=new_trade.orderStatus.status,
        )
        self.journal.update_order_status(pos.stop_row_id, "Cancelled")

        pos.stop_order = new_order
        pos.stop_row_id = new_row_id
        pos.current_stop_price = new_price
        self._wire_stop_fill(pos, new_trade)

    def _resize_stop_qty(self, pos: ManagedPosition, new_qty: int) -> None:
        pos.stop_order.totalQuantity = new_qty
        self.ib.placeOrder(pos.contract, pos.stop_order)
        self.journal.update_order_price(pos.stop_row_id, qty=new_qty)

    def _on_target_fill(self, pos: ManagedPosition, fill) -> None:
        filled_qty = fill.execution.shares
        pos.remaining_qty = max(0, pos.remaining_qty - filled_qty)
        if pos.remaining_qty <= 0:
            self._close_out(pos, cancel_stop=True, cancel_target=False)
            return
        # Resize the stop down after ANY partial target fill (not just a
        # scale_out tier) -- shares that already left via a target fill
        # must not stay covered by a stop still sized for the full lot.
        self._resize_stop_qty(pos, pos.remaining_qty)
        logger.info("Target fill for %s: stop resized to %d shares", pos.symbol, pos.remaining_qty)

    def _on_stop_fill(self, pos: ManagedPosition, fill) -> None:
        filled_qty = fill.execution.shares
        pos.remaining_qty = max(0, pos.remaining_qty - filled_qty)
        if pos.remaining_qty <= 0:
            self._close_out(pos, cancel_stop=False, cancel_target=True)

    def _close_out(self, pos: ManagedPosition, cancel_stop: bool, cancel_target: bool) -> None:
        """Position is flat -- best-effort cancel whichever counterpart
        order(s) are still resting (a no-op if IBKR's own OCA link already
        cancelled one) and stop tracking it."""
        if cancel_stop:
            self.ib.cancelOrder(pos.stop_order)
        if cancel_target:
            for target_order in pos.target_orders:
                self.ib.cancelOrder(target_order)
        self._untrack(pos)
