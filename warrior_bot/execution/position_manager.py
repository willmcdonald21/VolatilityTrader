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
    current_stop_price: float
    target_order: Order | None
    target_role: str
    breakeven_done: bool = False
    trailing_active: bool = False


class PositionManager:
    """Reacts to bar updates and fill events on already-submitted brackets
    to apply breakeven, trailing-stop, and scale-out management.
    `OrderManager` only places orders and journals fills/status — it never
    reacts to them, so this is the one place post-entry management lives.

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
        self._positions: dict[str, ManagedPosition] = {}

    def track(
        self,
        contract: Contract,
        signal: Signal,
        signal_id: int,
        stop_trade: Trade,
        stop_row_id: int,
        target_trade: Trade,
        target_role: str,
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
            target_order=target_trade.order,
            target_role=target_role,
        )
        self._positions[signal.symbol] = pos

        def on_target_fill(t: Trade, fill) -> None:
            self._on_target_fill(pos, fill)

        target_trade.fillEvent += on_target_fill
        self._wire_stop_fill(pos, stop_trade)

    def _wire_stop_fill(self, pos: ManagedPosition, trade: Trade) -> None:
        """Shared by track() (initial stop) and _replace_stop_order() (every
        replacement stop) -- cancel-and-replace swaps pos.stop_order for a
        brand-new Trade/Order, so its fillEvent has to be re-wired the same
        way each time or a fill on the replacement stop would never be
        noticed."""

        def on_stop_fill(t: Trade, fill) -> None:
            self._on_stop_fill(pos, fill)

        trade.fillEvent += on_stop_fill

    def on_bar(self, ctx: SymbolContext) -> None:
        pos = self._positions.get(ctx.symbol)
        if pos is None:
            return
        last_price = ctx.last_price
        if last_price is None:
            return

        if self.config.reversal_exit.enabled and self._check_reversal_exit(pos, ctx):
            return  # position is now flat -- nothing else to evaluate this bar

        if not pos.breakeven_done and self.config.breakeven.enabled:
            self._check_breakeven(pos, last_price)

        if pos.breakeven_done and self.config.trailing.enabled:
            self._check_trailing(pos, ctx, last_price)

    def clear(self) -> None:
        """Drops all tracked positions with no IBKR side effects — used
        after a kill-switch/auto-flatten pass that already cancelled and
        flattened everything directly."""
        self._positions.clear()

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
        if just_activated and pos.target_role == "target" and pos.target_order is not None:
            self.ib.cancelOrder(pos.target_order)
            pos.target_order = None
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
        if pos.target_order is not None:
            self.ib.cancelOrder(pos.target_order)
        order = MarketOrder("SELL", pos.remaining_qty)
        self.ib.placeOrder(pos.contract, order)
        reason_str = ",".join(reasons)
        logger.warning("Reversal exit for %s: %s (qty=%d)", pos.symbol, reason_str, pos.remaining_qty)
        self.journal.record_kill_switch_event(
            triggered_by=f"reversal_exit:{pos.symbol}:{reason_str}", action_taken="market_exit_position"
        )
        self._positions.pop(pos.symbol, None)

    def _replace_stop_order(self, pos: ManagedPosition, new_stop_price: float) -> None:
        """Cancel-and-replace instead of in-place price revision: IBKR
        rejects in-place revisions on OCA-grouped and/or partially-filled
        orders with error 10326 ("OCA group revision is not allowed"), and
        ib_async's own openOrder callback overwrites trade.order.auxPrice/
        .lmtPrice in place on any broadcast (including IBKR's own
        anti-crossing repricing on error 399), so pos.stop_order can never
        be trusted as a read-back source of truth -- pos.current_stop_price
        is the only value this class treats as authoritative for the
        monotonic "stop only tightens" invariant.

        Re-links OCA with pos.target_order if it's still live (breakeven
        case, pre-trailing-activation) using target_order.ocaGroup. If
        target_order is None (trailing already cancelled it) or was never
        OCA-linked (scale_out target_role -- bracket_builder deliberately
        never OCA-links a scale-out leg with the stop), ocaGroup is falsy
        and no relink is attempted, so the replacement stop is correctly
        standalone.
        """
        new_stop_price = round_to_tick(new_stop_price)
        action = pos.stop_order.action
        is_stop_limit = getattr(pos.stop_order, "orderType", None) == "STP LMT"

        limit_price = None
        if is_stop_limit:
            # keep the limit offset in the same direction bracket_builder
            # used when the order was first built, so trailing/breakeven
            # moves don't drift the limit's protective distance
            if action == "SELL":
                limit_price = round_to_tick(new_stop_price * (1 - self.stop_limit_offset_pct / 100.0))
            else:
                limit_price = round_to_tick(new_stop_price * (1 + self.stop_limit_offset_pct / 100.0))
            new_order = StopLimitOrder(
                action,
                pos.remaining_qty,
                lmtPrice=limit_price,
                stopPrice=new_stop_price,
                orderId=self.ib.client.getReqId(),
                transmit=True,
                outsideRth=True,
                tif="DAY",
            )
        else:
            # not hit in production today (bracket_builder only ever builds
            # StopLimitOrder) -- kept for parity with the branch this replaced
            new_order = StopOrder(
                action,
                pos.remaining_qty,
                stopPrice=new_stop_price,
                orderId=self.ib.client.getReqId(),
                transmit=True,
                outsideRth=True,
                tif="DAY",
            )

        if pos.target_order is not None and pos.target_order.ocaGroup:
            IB.oneCancelsAll([new_order], pos.target_order.ocaGroup, ocaType=1)

        self.ib.cancelOrder(pos.stop_order)
        new_trade = self.ib.placeOrder(pos.contract, new_order)

        new_row_id = self.journal.record_order(
            signal_id=pos.signal_id,
            ib_order_id=new_order.orderId,
            role="stop",
            action=new_order.action,
            qty=new_order.totalQuantity,
            order_type=new_order.orderType,
            limit_price=limit_price,
            stop_price=new_stop_price,
            oca_group=new_order.ocaGroup or None,
            status=new_trade.orderStatus.status,
        )
        self.journal.update_order_status(pos.stop_row_id, "Cancelled")

        pos.stop_order = new_order
        pos.stop_row_id = new_row_id
        pos.current_stop_price = new_stop_price
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
        if pos.target_role == "scale_out":
            self._resize_stop_qty(pos, pos.remaining_qty)
            logger.info("Scale-out fill for %s: stop resized to %d shares", pos.symbol, pos.remaining_qty)

    def _on_stop_fill(self, pos: ManagedPosition, fill) -> None:
        filled_qty = fill.execution.shares
        pos.remaining_qty = max(0, pos.remaining_qty - filled_qty)
        if pos.remaining_qty <= 0:
            self._close_out(pos, cancel_stop=False, cancel_target=True)

    def _close_out(self, pos: ManagedPosition, cancel_stop: bool, cancel_target: bool) -> None:
        """Position is flat -- best-effort cancel whichever counterpart
        order is still resting (a no-op if IBKR's own OCA link already
        cancelled it) and stop tracking it."""
        if cancel_stop:
            self.ib.cancelOrder(pos.stop_order)
        if cancel_target and pos.target_order is not None:
            self.ib.cancelOrder(pos.target_order)
        self._positions.pop(pos.symbol, None)
