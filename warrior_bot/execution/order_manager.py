from __future__ import annotations

import logging
import math

from ib_async import IB, Contract, Trade

from warrior_bot.config import ExecutionConfig, ExitsConfig, NotificationsConfig
from warrior_bot.execution.bracket_builder import Bracket, build_bracket
from warrior_bot.execution.position_manager import PositionManager
from warrior_bot.notify.discord import build_pnl_message, send_discord_message
from warrior_bot.persistence.journal import Journal
from warrior_bot.risk.account_state import AccountState
from warrior_bot.signals.signal import Signal
from warrior_bot.utils.rounding import round_to_tick

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
        profit_tiers = self._profit_tier_specs(signal, quantity)
        bracket = build_bracket(
            self.ib,
            signal,
            quantity,
            profit_tiers=profit_tiers,
            stop_limit_offset_pct=self.execution_config.stop_limit_offset_pct,
        )
        role_by_order_id = {bracket.parent.orderId: "parent", bracket.stop_loss.orderId: "stop"}
        for take_profit, role in zip(bracket.take_profits, bracket.target_roles):
            role_by_order_id[take_profit.orderId] = role

        parent_trade: Trade | None = None
        stop_trade: Trade | None = None
        stop_row_id: int | None = None
        target_trades: list[Trade] = []
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
            self._attach_tracking(trade, row_id, role, signal.entry_price)
            if role == "parent":
                parent_trade = trade
            elif role == "stop":
                stop_trade, stop_row_id = trade, row_id
            else:
                target_trades.append(trade)

        logger.info(
            "Submitted bracket for %s: qty=%s entry=%.4f stop=%.4f tiers=%s",
            signal.symbol,
            quantity,
            signal.entry_price,
            signal.stop_price,
            profit_tiers or [(quantity, signal.target_price)],
        )

        assert parent_trade is not None and stop_trade is not None and stop_row_id is not None
        self.position_manager.track(
            contract,
            signal,
            signal_id=signal_id,
            parent_trade=parent_trade,
            stop_trade=stop_trade,
            stop_row_id=stop_row_id,
            target_trades=target_trades,
            target_roles=bracket.target_roles,
        )
        return bracket

    def _profit_tier_specs(self, signal: Signal, quantity: int) -> list[tuple[int, float]]:
        """(qty, price) per configured profit tier, each qty a floor of `pct`
        of the *original* position size. Skips any tier that floors to zero
        (e.g. a very small position) -- build_bracket falls back to a single
        full-quantity target at signal.target_price if the resulting list
        ends up empty."""
        specs = []
        for tier in self.exits_config.profit_tiers:
            qty = math.floor(quantity * tier.pct)
            if qty <= 0:
                continue
            price = round_to_tick(signal.entry_price + signal.risk_per_share * tier.r_multiple)
            specs.append((qty, price))
        return specs

    def _attach_tracking(self, trade: Trade, row_id: int, role: str, entry_price: float | None = None) -> None:
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
                pct_str = ""
                if role == "scale_out" and entry_price:
                    pct_change = (fill.execution.price - entry_price) / entry_price * 100.0
                    pct_str = f" ({pct_change:+.1f}% from entry)"
                send_discord_message(
                    f"💰 {label} {trade.contract.symbol} "
                    f"{fill.execution.shares:g} @ ${fill.execution.price:.2f}{pct_str}{pnl_str}",
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
