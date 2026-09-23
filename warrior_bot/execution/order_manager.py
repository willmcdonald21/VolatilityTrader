from __future__ import annotations

import asyncio
import logging
import math

from ib_async import IB, Contract, Trade

from warrior_bot.config import ExecutionConfig, ExitsConfig, NotificationsConfig
from warrior_bot.execution.bracket_builder import Bracket, build_bracket
from warrior_bot.execution.position_manager import PositionManager
from warrior_bot.notify.discord import (
    build_entry_summary_embed,
    build_pnl_message,
    send_discord_embed,
    send_discord_message,
)
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

# Same rationale as position_manager.py's _STOP_RESIZE_DEBOUNCE_SECONDS: a
# single parent order for this bot's thin/low-float universe routinely
# lands as a burst of several small partial fills a fraction of a second
# apart (confirmed live, 2026-09-22: JTAI's 1,482-share entry arrived as 6
# separate fills, one 842 shares, the rest 100-200). Without coalescing,
# the trade_activity_summary channel would get one "NEW POSITION" embed per
# partial fill instead of one per entry.
_ENTRY_SUMMARY_DEBOUNCE_SECONDS = 1.5


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
        trading_mode: str = "paper",
    ):
        self.ib = ib
        self.journal = journal
        self.exits_config = exits_config
        self.position_manager = position_manager
        self.execution_config = execution_config or ExecutionConfig()
        self.notifications_config = notifications_config or NotificationsConfig()
        self.account_state = account_state
        # Only for the trade_activity_summary embed's footer (build_entry_summary_embed) --
        # never used for any trading decision, so a bad value here means a
        # mislabeled Discord message, not a wrong trade.
        self.trading_mode = trading_mode
        self._order_row_ids: dict[int, int] = {}  # ib order id -> journal orders.id
        # signal_id -> accumulator for the trade_activity_summary embed (see
        # _accumulate_entry_fill/_send_entry_summary). Kept around (not
        # popped) after a summary is sent so a later pyramid add-on on the
        # same symbol can read this lot's finished avg_price as "prior avg".
        self._entry_fill_state: dict[int, dict] = {}

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

        self._entry_fill_state[signal_id] = {
            "symbol": signal.symbol,
            "strategy": signal.strategy,
            "stop_price": signal.stop_price,
            "trim_targets": self._trim_target_display(signal, quantity),
            "qty": 0.0,
            "notional": 0.0,
            "avg_price": None,
            "task": None,
        }

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
            self._attach_tracking(trade, row_id, role, signal.entry_price, signal_id=signal_id)
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

    def _trim_target_display(self, signal: Signal, quantity: int) -> list[tuple[float, float]]:
        """(pct_of_position, price) per configured profit tier, for the
        trade_activity_summary embed (build_entry_summary_embed) -- mirrors
        _profit_tier_specs' own skip-if-floors-to-zero filtering and
        empty-list fallback exactly, so what's displayed always matches the
        real orders _profit_tier_specs produced for this same (signal,
        quantity), just carrying the configured fraction through instead of
        an absolute share count."""
        targets = [
            (tier.pct, round_to_tick(signal.entry_price + signal.risk_per_share * tier.r_multiple))
            for tier in self.exits_config.profit_tiers
            if math.floor(quantity * tier.pct) > 0
        ]
        return targets or [(1.0, signal.target_price)]

    def _attach_tracking(
        self,
        trade: Trade,
        row_id: int,
        role: str,
        entry_price: float | None = None,
        signal_id: int | None = None,
    ) -> None:
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
            # Not realized_pnl (above): IBKR's paper simulator reports
            # commissionReport.realizedPNL as ~0.0 on every genuine closing
            # fill (see account_state.py's daily_realized_pnl docstring --
            # confirmed against 2,500+ fills), which is why that method
            # recomputes P&L from raw fill prices instead of trusting the
            # field. This trade-level notification had the same bug: every
            # closing fill showed "+$0.00" here while Daily P&L (fed from
            # the already-fixed daily_realized_pnl) correctly kept dropping
            # -- two P&L sources in one message, only one of them honest.
            # Computed the same way: (exit - entry) * shares, net of
            # commission. entry_price is this specific lot's own entry (the
            # signal that opened it), not a symbol-wide blend, so an add-on
            # lot's trim/stop is priced against its own cost, not the
            # other lot's -- correct even with two lots open at once.
            trade_pnl = None
            if role != "parent" and entry_price is not None:
                commission_cost = commission if commission is not None and abs(commission) < 1e15 else 0.0
                trade_pnl = (fill.execution.price - entry_price) * fill.execution.shares - commission_cost
            if self.notifications_config.enabled and self.notifications_config.notify_on_fill:
                label = _FILL_LABELS.get(role, "SELL")
                pnl_str = f" (P&L ${trade_pnl:.2f})" if trade_pnl is not None else ""
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
                trade_pnl is not None
                and self.notifications_config.enabled
                and self.notifications_config.notify_on_pnl
            ):
                daily_pnl = self.account_state.snapshot().daily_realized_pnl if self.account_state else trade_pnl
                send_discord_message(
                    build_pnl_message(trade.contract.symbol, trade_pnl, daily_pnl), channel="pnl"
                )
            if role == "parent" and signal_id is not None:
                self._accumulate_entry_fill(signal_id, fill.execution.shares, fill.execution.price)

        trade.statusEvent += on_status
        trade.fillEvent += on_fill

    def _accumulate_entry_fill(self, signal_id: int, fill_qty: float, fill_price: float) -> None:
        """Folds one parent-order partial fill into signal_id's running
        total and (re)schedules the debounced trade_activity_summary embed
        -- see _ENTRY_SUMMARY_DEBOUNCE_SECONDS. A no-op if this signal_id's
        accumulator is missing (only resync_open_orders' path can leave it
        unset, for a parent order still working across a process restart --
        that edge case just doesn't get a summary embed, journaling and the
        raw trade_activity line are unaffected)."""
        state = self._entry_fill_state.get(signal_id)
        if state is None:
            return
        state["qty"] += fill_qty
        state["notional"] += fill_qty * fill_price
        if state["task"] is not None:
            state["task"].cancel()

        async def _debounced() -> None:
            try:
                await asyncio.sleep(_ENTRY_SUMMARY_DEBOUNCE_SECONDS)
            except asyncio.CancelledError:
                return
            state["task"] = None
            self._send_entry_summary(signal_id)

        state["task"] = asyncio.ensure_future(_debounced())

    def _send_entry_summary(self, signal_id: int) -> None:
        if not (self.notifications_config.enabled and self.notifications_config.notify_on_entry_summary):
            return
        state = self._entry_fill_state.get(signal_id)
        if state is None or state["qty"] <= 0:
            return
        state["avg_price"] = state["notional"] / state["qty"]

        prior_qty = prior_avg_price = None
        other = self.position_manager.other_open_lot(state["symbol"], exclude_signal_id=signal_id)
        if other is not None:
            prior_state = self._entry_fill_state.get(other.signal_id)
            if prior_state is not None and prior_state.get("avg_price") is not None:
                prior_qty, prior_avg_price = prior_state["qty"], prior_state["avg_price"]

        embed = build_entry_summary_embed(
            symbol=state["symbol"],
            strategy=state["strategy"],
            qty=state["qty"],
            avg_price=state["avg_price"],
            stop_price=state["stop_price"],
            trim_targets=state["trim_targets"],
            mode=self.trading_mode,
            prior_qty=prior_qty,
            prior_avg_price=prior_avg_price,
        )
        send_discord_embed(embed, channel="trade_activity_summary")

    def resync_open_orders(self, force: bool = False, skip_order_ids: frozenset[int] = frozenset()) -> None:
        """Re-attaches fill/status tracking (journal writes + Discord
        notify_on_fill/notify_on_pnl) to orders that are still resting at
        IBKR from before this process started -- covers both a supervisor
        restart and a fresh `python -m warrior_bot.main` launch.

        `_attach_tracking` normally runs once, right when `submit_signal`
        places an order, wiring listeners onto that specific in-memory
        `Trade` object. Those listeners don't survive a process restart --
        the order keeps resting and filling at IBKR, but the *new* process
        has no listener on it, so fills silently stop being journaled and
        stop notifying Discord (confirmed live for VHUB/SCNI/SKDD,
        2026-09-14). `ib.openTrades()` right after connect already contains
        Trade objects for this same clientId's still-open orders (IBKR's
        default reqOpenOrders on connect is scoped to the connecting
        clientId), so this only needs to re-attach, not replace, them.

        `force=True` is for the *same-process reconnect* case (see
        main.py's `_on_connected`), which the `_order_row_ids` guard below
        would otherwise defeat: this instance already knows every orderId
        it has ever placed, but ib_async's own reconnect handling
        (IB.disconnect() -> wrapper.reset()) throws away its Trade objects
        and builds fresh ones, so a listener wired on the pre-reconnect
        Trade is just as dead here as it would be after a process restart
        -- `_order_row_ids` still containing the id doesn't mean the
        listener attached to it is still live. `force=True` re-attaches
        regardless of that dict, which is harmless/idempotent (the old
        listener is on an abandoned object nobody feeds events into
        anymore). `skip_order_ids` excludes orders
        `PositionManager.resync_after_reconnect` already re-wired itself
        (stop/target orders only) -- without it, a reconnect resync would
        double-journal the next fill on any of those, the same bug already
        fixed once in track()/_wire_stop_fill for the non-reconnect path.

        Deliberately does NOT touch `PositionManager`'s own management
        listeners (breakeven/trailing/reversal-exit/qty tracking) --
        that's `PositionManager.resync_after_reconnect`'s job, called
        separately. Orders placed manually outside the bot (never in the
        journal) are skipped -- there's no signal_id to attach fills to."""
        resynced = 0
        for trade in self.ib.openTrades():
            order_id = trade.order.orderId
            if order_id in skip_order_ids:
                continue
            if not force and order_id in self._order_row_ids:
                continue  # already tracked by this process (placed after resync ran)
            found = self.journal.find_order_by_ib_order_id(order_id)
            if found is None:
                continue
            self._order_row_ids[order_id] = found["row_id"]
            self._attach_tracking(trade, found["row_id"], found["role"], found["entry_price"])
            resynced += 1
        if resynced:
            logger.info(
                "Resynced fill/status tracking for %d pre-existing open order(s) "
                "(journal + Discord notifications restored for these)",
                resynced,
            )

    def cancel_all(self) -> None:
        for trade in self.ib.openTrades():
            self.ib.cancelOrder(trade.order)
