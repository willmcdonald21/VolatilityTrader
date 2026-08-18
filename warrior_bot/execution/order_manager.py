from __future__ import annotations

import logging

from ib_async import IB, Contract, Trade

from warrior_bot.config import ExecutionConfig, ExitsConfig, NotificationsConfig
from warrior_bot.execution.bracket_builder import Bracket, build_bracket
from warrior_bot.execution.position_manager import PositionManager
from warrior_bot.notify.discord import build_pnl_message, send_discord_message
from warrior_bot.persistence.journal import Journal
from warrior_bot.risk.account_state import AccountState
from warrior_bot.signals.signal import Signal

logger = logging.getLogger("warrior_bot.execution.order_manager")

# order_manager.py's `role` -> the label used in trade_activity messages.
# "target"/"stop" both mean "the position (or what's left of it) closed" --
# a full sell; "scale_out" is a partial close ("trim"); "parent" is the
# opening entry.
_FILL_LABELS = {"parent": "BUY", "scale_out": "TRIM"}


class OrderManager:
    """Submits brackets and keeps the journal in sync with IBKR's fill/status
    events. Does not maintain its own position/PnL truth — that's
    `risk.account_state.AccountState`'s job; this class only tracks the
    orders it itself placed, for journaling and OCA bookkeeping. Post-entry
    position management (breakeven/trailing/scale-out) is `PositionManager`'s
    job, registered once here right after a bracket is placed."""

    def __init__(
        self,
        ib: IB,
        journal: Journal,
        exits_config: ExitsConfig,
        position_manager: PositionManager,
        execution_config: ExecutionConfig | None = None,
        notifications_config: NotificationsConfig | None = None,
        account_state: AccountState | None = None,
    ):
        self.ib = ib
        self.journal = journal
        self.exits_config = exits_config
        self.position_manager = position_manager
        self.execution_config = execution_config or ExecutionConfig()
        self.notifications_config = notifications_config or NotificationsConfig()
        self.account_state = account_state
        self._order_row_ids: dict[int, int] = {}  # ib order id -> journal orders.id

    def submit_signal(self, contract: Contract, signal: Signal, quantity: int, signal_id: int) -> Bracket:
        scale_out_qty, scale_out_price = self._scale_out_params(signal, quantity)
        bracket = build_bracket(
            self.ib,
            signal,
            quantity,
            scale_out_qty=scale_out_qty,
            scale_out_price=scale_out_price,
            stop_limit_offset_pct=self.execution_config.stop_limit_offset_pct,
        )
        role_by_order_id = {
            bracket.parent.orderId: "parent",
            bracket.take_profit.orderId: bracket.target_role,
            bracket.stop_loss.orderId: "stop",
        }

        trades_by_role: dict[str, tuple[Trade, int]] = {}
        for order in bracket.orders:
            trade = self.ib.placeOrder(contract, order)
            role = role_by_order_id[order.orderId]
            row_id = self.journal.record_order(
                signal_id=signal_id,
                ib_order_id=order.orderId,
                role=role,
                action=order.action,
                qty=order.totalQuantity,
                order_type=order.orderType,
                limit_price=getattr(order, "lmtPrice", None),
                stop_price=getattr(order, "auxPrice", None),
                oca_group=order.ocaGroup or None,
                status=trade.orderStatus.status,
            )
            self._order_row_ids[order.orderId] = row_id
            self._attach_tracking(trade, row_id, role)
            trades_by_role[role] = (trade, row_id)

        logger.info(
            "Submitted bracket for %s: qty=%s entry=%.4f stop=%.4f target=%.4f",
            signal.symbol,
            quantity,
            signal.entry_price,
            signal.stop_price,
            signal.target_price,
        )

        stop_trade, stop_row_id = trades_by_role["stop"]
        target_trade, _ = trades_by_role[bracket.target_role]
        self.position_manager.track(
            contract,
            signal,
            stop_trade=stop_trade,
            stop_row_id=stop_row_id,
            target_trade=target_trade,
            target_role=bracket.target_role,
        )
        return bracket

    def _scale_out_params(self, signal: Signal, quantity: int) -> tuple[int | None, float | None]:
        cfg = self.exits_config.scale_out
        if not cfg.enabled:
            return None, None
        scale_out_qty = round(quantity * cfg.pct)
        if scale_out_qty <= 0 or scale_out_qty >= quantity:
            return None, None
        scale_out_price = signal.entry_price + signal.risk_per_share * cfg.r_multiple
        return scale_out_qty, scale_out_price

    def _attach_tracking(self, trade: Trade, row_id: int, role: str) -> None:
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
            if self.notifications_config.enabled and self.notifications_config.notify_on_fill:
                label = _FILL_LABELS.get(role, "SELL")
                pnl_str = f" (P&L ${realized_pnl:.2f})" if realized_pnl is not None else ""
                send_discord_message(
                    f"💰 {label} {trade.contract.symbol} "
                    f"{fill.execution.shares:g} @ ${fill.execution.price:.2f}{pnl_str}",
                    channel="trade_activity",
                )
            if (
                realized_pnl is not None
                and self.notifications_config.enabled
                and self.notifications_config.notify_on_pnl
            ):
                daily_pnl = self.account_state.snapshot().daily_realized_pnl if self.account_state else realized_pnl
                send_discord_message(
                    build_pnl_message(trade.contract.symbol, realized_pnl, daily_pnl), channel="pnl"
                )

        trade.statusEvent += on_status
        trade.fillEvent += on_fill

    def cancel_all(self) -> None:
        for trade in self.ib.openTrades():
            self.ib.cancelOrder(trade.order)
