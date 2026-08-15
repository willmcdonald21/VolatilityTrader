from __future__ import annotations

import logging
from dataclasses import dataclass

from ib_async import IB, Contract, Order, Trade

from warrior_bot.config import ExitsConfig
from warrior_bot.persistence.journal import Journal
from warrior_bot.signals.signal import Signal
from warrior_bot.strategies.base_strategy import SymbolContext
from warrior_bot.strategies.indicators import trailing_candidate

logger = logging.getLogger("warrior_bot.execution.position_manager")


@dataclass
class ManagedPosition:
    symbol: str
    contract: Contract
    signal: Signal
    remaining_qty: int
    stop_order: Order
    stop_row_id: int
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

    def __init__(self, ib: IB, journal: Journal, config: ExitsConfig):
        self.ib = ib
        self.journal = journal
        self.config = config
        self._positions: dict[str, ManagedPosition] = {}

    def track(
        self,
        contract: Contract,
        signal: Signal,
        stop_trade: Trade,
        stop_row_id: int,
        target_trade: Trade,
        target_role: str,
    ) -> None:
        pos = ManagedPosition(
            symbol=signal.symbol,
            contract=contract,
            signal=signal,
            remaining_qty=int(stop_trade.order.totalQuantity),
            stop_order=stop_trade.order,
            stop_row_id=stop_row_id,
            target_order=target_trade.order,
            target_role=target_role,
        )
        self._positions[signal.symbol] = pos

        def on_target_fill(t: Trade, fill) -> None:
            self._on_target_fill(pos, fill)

        def on_stop_fill(t: Trade, fill) -> None:
            self._on_stop_fill(pos, fill)

        target_trade.fillEvent += on_target_fill
        stop_trade.fillEvent += on_stop_fill

    def on_bar(self, ctx: SymbolContext) -> None:
        pos = self._positions.get(ctx.symbol)
        if pos is None:
            return
        last_price = ctx.last_price
        if last_price is None:
            return

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
        if entry > pos.stop_order.auxPrice:
            self._modify_stop_price(pos, entry)
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
        current_stop = pos.stop_order.auxPrice
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

        self._modify_stop_price(pos, new_stop)
        logger.info("Trailing: moved stop for %s to %.4f", pos.symbol, new_stop)

    def _modify_stop_price(self, pos: ManagedPosition, new_price: float) -> None:
        pos.stop_order.auxPrice = new_price
        self.ib.placeOrder(pos.contract, pos.stop_order)
        self.journal.update_order_price(pos.stop_row_id, stop_price=new_price)

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
