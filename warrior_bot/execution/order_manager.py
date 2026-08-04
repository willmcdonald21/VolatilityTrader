from __future__ import annotations

import logging

from ib_async import IB, Contract, Trade

from warrior_bot.execution.bracket_builder import Bracket, build_bracket
from warrior_bot.persistence.journal import Journal
from warrior_bot.signals.signal import Signal

logger = logging.getLogger("warrior_bot.execution.order_manager")


class OrderManager:
    """Submits brackets and keeps the journal in sync with IBKR's fill/status
    events. Does not maintain its own position/PnL truth — that's
    `risk.account_state.AccountState`'s job; this class only tracks the
    orders it itself placed, for journaling and OCA bookkeeping."""

    def __init__(self, ib: IB, journal: Journal):
        self.ib = ib
        self.journal = journal
        self._order_row_ids: dict[int, int] = {}  # ib order id -> journal orders.id

    def submit_signal(self, contract: Contract, signal: Signal, quantity: int, signal_id: int) -> Bracket:
        bracket = build_bracket(self.ib, signal, quantity)
        role_by_order_id = {
            bracket.parent.orderId: "parent",
            bracket.take_profit.orderId: "target",
            bracket.stop_loss.orderId: "stop",
        }

        for order in bracket.orders:
            trade = self.ib.placeOrder(contract, order)
            row_id = self.journal.record_order(
                signal_id=signal_id,
                ib_order_id=order.orderId,
                role=role_by_order_id[order.orderId],
                action=order.action,
                qty=order.totalQuantity,
                order_type=order.orderType,
                limit_price=getattr(order, "lmtPrice", None),
                stop_price=getattr(order, "auxPrice", None),
                oca_group=order.ocaGroup or None,
                status=trade.orderStatus.status,
            )
            self._order_row_ids[order.orderId] = row_id
            self._attach_tracking(trade, row_id)

        logger.info(
            "Submitted bracket for %s: qty=%s entry=%.4f stop=%.4f target=%.4f",
            signal.symbol,
            quantity,
            signal.entry_price,
            signal.stop_price,
            signal.target_price,
        )
        return bracket

    def _attach_tracking(self, trade: Trade, row_id: int) -> None:
        def on_status(t: Trade) -> None:
            self.journal.update_order_status(row_id, t.orderStatus.status)

        def on_fill(t: Trade, fill) -> None:
            realized_pnl = None
            commission = None
            if fill.commissionReport is not None:
                commission = fill.commissionReport.commission
                # UNSET_DOUBLE sentinel on the opening leg of a round trip; see account_state.py
                pnl = fill.commissionReport.realizedPNL
                if pnl is not None and abs(pnl) < 1e15:
                    realized_pnl = pnl
            self.journal.record_fill(
                order_row_id=row_id,
                ib_order_id=trade.order.orderId,
                fill_qty=fill.execution.shares,
                fill_price=fill.execution.price,
                commission=commission,
                realized_pnl=realized_pnl,
            )

        trade.statusEvent += on_status
        trade.fillEvent += on_fill

    def cancel_all(self) -> None:
        for trade in self.ib.openTrades():
            self.ib.cancelOrder(trade.order)
