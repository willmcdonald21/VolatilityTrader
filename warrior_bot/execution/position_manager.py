from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ib_async import IB, Contract, MarketOrder, Order, StopLimitOrder, StopOrder, Trade

from warrior_bot.config import ExitsConfig, NotificationsConfig
from warrior_bot.notify.discord import send_discord_message
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

# Thin/low-float symbols (this bot's entire universe) routinely fill a
# single parent order in a burst of dozens of tiny partial fills a few
# hundred milliseconds apart (confirmed live, 2026-09-15: MTEN's 3,479-share
# entry arrived as 23 separate fills across ~6 seconds). Resizing the stop
# on every one of those individually means 20+ cancel-and-replace round
# trips to the broker for a single logical entry. Coalescing fills that
# land within this window into one replace, sized off whatever
# remaining_qty is by the time the debounce fires, collapses that back down
# to (usually) one stop order per burst while still protecting the full
# filled quantity moments after the burst ends.
_STOP_RESIZE_DEBOUNCE_SECONDS = 1.5


@dataclass
class ManagedPosition:
    symbol: str
    contract: Contract
    signal: Signal
    signal_id: int
    remaining_qty: int
    parent_order: Order
    stop_order: Order
    stop_row_id: int
    # Authoritative current stop price, tracked here rather than read back
    # off stop_order.auxPrice -- ib_async's own openOrder callback
    # overwrites that attribute in place on any IBKR broadcast, so it can't
    # be trusted as "the price we last set."
    current_stop_price: float
    target_orders: list[Order]
    target_roles: list[str]  # parallel to target_orders
    # Set once the parent (entry) order records its first fill. Breakeven,
    # trailing, and reversal-exit must never act before this is true --
    # doing so would manage/replace protection for shares not actually
    # owned yet (see _replace_stop_order's parentId note for why that's
    # dangerous specifically for stop revisions).
    entry_filled: bool = False
    # True once the parent order has no shares left to fill. Until then,
    # remaining_qty hitting 0 (e.g. a fast tier fill selling everything
    # bought *so far*) does not mean the position is actually flat -- the
    # still-working parent can go on to fill more shares later. Confirmed
    # live (BLSG, 2026-09-14): closing out and untracking on remaining_qty
    # <= 0 without checking this orphaned 959 later-filled shares with no
    # listener left to protect them.
    parent_done: bool = False
    breakeven_done: bool = False
    trailing_active: bool = False
    # When the bracket was submitted -- the clock cancel_stale_entries runs
    # against, so a working entry can't outlive the setup that justified it.
    submitted_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # Pending debounced stop-resize (see _schedule_stop_resize) -- cancelled
    # and rescheduled on every fill so a burst only replaces the stop once,
    # after fills stop arriving for _STOP_RESIZE_DEBOUNCE_SECONDS.
    resize_task: asyncio.Task | None = None


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

    def __init__(
        self,
        ib: IB,
        journal: Journal,
        config: ExitsConfig,
        stop_limit_offset_pct: float = 0.5,
        notifications_config: NotificationsConfig | None = None,
    ):
        self.ib = ib
        self.journal = journal
        self.config = config
        self.stop_limit_offset_pct = stop_limit_offset_pct
        self.notifications_config = notifications_config or NotificationsConfig()
        self._positions: dict[str, list[ManagedPosition]] = {}

    def open_lot_count(self, symbol: str) -> int:
        return len(self._positions.get(symbol, []))

    def open_lot_strategies(self, symbol: str) -> set[str]:
        """Which strategy(ies) currently hold a lot in this symbol -- lets
        RiskManager tell "I'm adding to my own idea" (every existing lot is
        the same strategy as the new signal) apart from "a different idea
        just walked in on my position" (see cross_strategy_lot_conflict in
        risk_manager.py)."""
        return {pos.signal.strategy for pos in self._positions.get(symbol, [])}

    def seconds_since_first_entry(self, symbol: str) -> float | None:
        """Age of the symbol's oldest tracked lot, None if none is tracked."""
        lots = self._positions.get(symbol)
        if not lots:
            return None
        oldest = min(lot.submitted_at for lot in lots)
        return (datetime.now(timezone.utc) - oldest).total_seconds()

    def tracked_symbols(self) -> set[str]:
        return set(self._positions.keys())

    def other_open_lot(self, symbol: str, exclude_signal_id: int) -> ManagedPosition | None:
        """The other currently-tracked lot for `symbol`, if any, besides
        `exclude_signal_id` -- used by OrderManager's entry-summary notifier
        to detect a pyramid add-on (a second lot landing on a symbol that
        already has one open) and find the prior lot's signal_id to frame
        it as "added to position" rather than a fresh "new position"."""
        for pos in self._positions.get(symbol, []):
            if pos.signal_id != exclude_signal_id:
                return pos
        return None

    def lots_for_symbol(self, symbol: str) -> list[ManagedPosition]:
        """Every currently-tracked lot for `symbol` -- used by main.py to
        attribute an emergency-flatten fill (reconciliation watchdog or
        routine EOD flatten, see warrior_bot/utils/panic.py's
        on_order_placed hook) back to the signal_id(s) that opened it, so
        the exit actually lands in the journal instead of leaving that
        trade showing as "still open, $0 realized" forever."""
        return list(self._positions.get(symbol, []))

    def drop_symbol(self, symbol: str) -> None:
        """Used by main.py's position-reconciliation watchdog when IBKR's
        real position for `symbol` is flat but this class still shows
        tracked lots for it (stale local state -- e.g. a resync that
        couldn't resolve a filled-while-disconnected stop, see
        resync_after_reconnect). No IBKR side effects, matching clear()."""
        self._positions.pop(symbol, None)

    def track(
        self,
        contract: Contract,
        signal: Signal,
        signal_id: int,
        parent_trade: Trade,
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
            # Starts at 0, not the full intended order size -- this bot
            # trades exclusively thin/low-float stocks where the parent
            # entry order routinely takes minutes to fully fill (or never
            # fully fills). _wire_entry_fill below grows this with each
            # real partial fill, so it always reflects shares actually
            # held, never the size we merely intended to buy.
            remaining_qty=0,
            parent_order=parent_trade.order,
            stop_order=stop_trade.order,
            stop_row_id=stop_row_id,
            current_stop_price=signal.stop_price,
            target_orders=[t.order for t in target_trades],
            target_roles=list(target_roles),
        )
        self._positions.setdefault(signal.symbol, []).append(pos)

        self._wire_entry_fill(pos, parent_trade)

        for target_trade in target_trades:
            self._wire_target_fill(pos, target_trade)
        # journal_fill=False: this is the original bracket's stop_trade,
        # which OrderManager.submit_signal already ran through
        # _attach_tracking (journaling its fills there) before ever
        # calling track() -- journaling it again here double-counts every
        # fill on the original stop (confirmed live, 2026-09-14: every
        # stop-fill row in data/journal.sqlite3 for an unreplaced stop was
        # duplicated back-to-back). Only a *replacement* stop (see
        # _replace_stop_order) needs this path to journal at all.
        self._wire_stop_fill(pos, stop_trade, journal_fill=False)

    def _wire_entry_fill(self, pos: ManagedPosition, trade: Trade) -> None:
        """Separated from track() so resync_after_reconnect can re-wire the
        same handling onto a fresh Trade object for a parent order that
        hadn't finished filling before a reconnect (see that method)."""

        def on_entry_fill(t: Trade, fill) -> None:
            pos.entry_filled = True
            pos.remaining_qty += int(fill.execution.shares)
            # t.orderStatus.remaining reflects the parent's own state as of
            # *this* fill -- 0 means nothing is left to fill, ever. Until
            # that's true, a remaining_qty of 0 later on just means
            # everything bought *so far* has also been sold, not that the
            # position is done (see _close_out).
            pos.parent_done = t.orderStatus.remaining == 0
            # Keep the resting stop's size in step with shares actually
            # held so far -- without this, a breakeven/trailing move that
            # fires while the parent is still (partially) filling would
            # build its replacement from a remaining_qty that undercounts
            # true holdings, which is harmless (under-protects new
            # shares); the dangerous direction this guards against is
            # _replace_stop_order ever being handed a stale, too-large
            # quantity from before this fix (confirmed live: GVH went
            # short -1441 shares on 2026-09-14 when a replacement stop
            # was sized off the full intended 3943-share order while only
            # a fraction had actually filled). Cancel-and-replace (not a
            # resize-in-place) also means this self-heals correctly even
            # if the current stop was already cancelled by a prior
            # flat-for-now close_out below.
            self._schedule_stop_resize(pos)

        trade.fillEvent += on_entry_fill

    def _wire_stop_fill(self, pos: ManagedPosition, trade: Trade, journal_fill: bool = True) -> None:
        """Separated from track() so a replacement stop order (see
        _replace_stop_order) can re-wire the same fill handling onto its
        own fresh Trade object. A replacement stop's Trade never passes
        through OrderManager._attach_tracking (only the original bracket
        submission does), so journal_fill=True here is the only place a
        fill on a *revised* stop ever gets journaled -- without it, a stop
        that was cancelled-and-replaced even once would go fill-blind in
        data/journal.sqlite3 even though a real execution happened. The
        original bracket's stop_trade (see track() above) is the one
        exception -- OrderManager already journals that one, so track()
        passes journal_fill=False to avoid double-recording it."""
        trade.fillEvent += lambda t, fill: self._on_stop_fill(pos, t, fill, journal_fill=journal_fill)

    def _wire_target_fill(self, pos: ManagedPosition, trade: Trade) -> None:
        trade.fillEvent += lambda t, fill: self._on_target_fill(pos, fill)

    def resync_after_reconnect(self, ib: IB) -> set[int]:
        """Re-wires fill listeners for every tracked lot's stop/target/parent
        orders onto fresh Trade objects after an IBKR disconnect/reconnect.

        ib_async's IB.disconnect() calls wrapper.reset(), which wipes its
        internal trades/permId2Trade dicts -- any order that survives the
        reconnect at the broker gets a brand-new Trade object the first
        time ib_async hears about it again (via reqOpenOrders during
        reconnection), and any fillEvent listener wired onto the OLD Trade
        object never fires again, silently, with no error. Confirmed live,
        2026-09-16: two NRXS stop orders filled correctly at the broker
        just after a reconnect, invisibly to this class -- remaining_qty
        was never decremented and the lot was never untracked, so a later
        stop-resize placed a brand-new full-size duplicate stop on top of
        an already-flat lot, which itself later filled for real, doubling
        the exit into a naked short that sat unprotected for ~3h53m.

        Returns the set of stop/target orderIds re-wired here, so callers
        can tell OrderManager.resync_open_orders not to also re-attach its
        own journaling listener to them -- this class always journals
        whatever it resyncs (journal_fill=True unconditionally, unlike
        track()'s original-vs-replacement distinction), so there's no more
        "OrderManager already has a live listener on this one" asymmetry
        to preserve once a reconnect has killed every pre-existing
        listener uniformly. The parent order is deliberately excluded from
        the returned set -- OrderManager always owns journaling entry
        fills; this class's own parent-fill listener only tracks qty/state
        and never journals, so there's no conflict there to avoid.

        Anything NOT found among ib.openTrades() (filled or cancelled
        entirely while disconnected -- not resolvable from listener
        re-wiring alone, since there's no fresh Trade object to attach to)
        is left as-is and logged. main.py's periodic position-reconciliation
        watchdog is the unconditional backstop that catches and flattens
        any resulting mismatch against IBKR's real position on its next
        cycle, regardless of why the mismatch happened -- deliberately not
        duplicated here, to keep this method's job to exactly what
        listener re-wiring can actually resolve."""
        claimed: set[int] = set()
        open_by_id = {trade.order.orderId: trade for trade in ib.openTrades()}

        for symbol, lots in list(self._positions.items()):
            for pos in list(lots):
                if not pos.entry_filled:
                    fresh_parent = open_by_id.get(pos.parent_order.orderId)
                    if fresh_parent is not None:
                        self._wire_entry_fill(pos, fresh_parent)
                    else:
                        logger.warning(
                            "Resync: parent order for %s (orderId=%d) not found after reconnect -- "
                            "entry-fill tracking may be stale; reconciliation watchdog will verify",
                            symbol,
                            pos.parent_order.orderId,
                        )

                fresh_stop = open_by_id.get(pos.stop_order.orderId)
                if fresh_stop is not None:
                    self._wire_stop_fill(pos, fresh_stop, journal_fill=True)
                    claimed.add(pos.stop_order.orderId)
                else:
                    logger.warning(
                        "Resync: stop order for %s (orderId=%d) not found after reconnect -- "
                        "may have filled or been cancelled while disconnected; "
                        "reconciliation watchdog will verify against IBKR's real position",
                        symbol,
                        pos.stop_order.orderId,
                    )

                for target_order in pos.target_orders:
                    fresh_target = open_by_id.get(target_order.orderId)
                    if fresh_target is not None:
                        self._wire_target_fill(pos, fresh_target)
                        claimed.add(target_order.orderId)

        if claimed:
            logger.info("Resync: re-wired fill tracking for %d order(s) after reconnect", len(claimed))
        return claimed

    def on_bar(self, ctx: SymbolContext) -> None:
        lots = self._positions.get(ctx.symbol)
        if not lots:
            return
        last_price = ctx.last_price
        if last_price is None:
            return

        for pos in list(lots):  # copy -- a reversal exit mutates the list mid-loop
            if not pos.entry_filled:
                # Nothing to protect yet -- breakeven/trailing/reversal-exit
                # would otherwise manage (and, for stop revisions, replace)
                # protection for shares that may never actually be owned.
                continue
            if pos.remaining_qty <= 0:
                # Flat for now but still tracked because the parent order
                # may yet fill more (see _close_out) -- nothing to manage
                # until that happens. Skipping this also avoids
                # _reversal_exit building a zero-quantity market order.
                continue

            if self.config.reversal_exit.enabled and self._check_reversal_exit(pos, ctx):
                continue  # this lot is now flat -- evaluate any remaining lot for this symbol

            if not pos.breakeven_done and self.config.breakeven.enabled:
                self._check_breakeven(pos, last_price)

            if pos.breakeven_done and self.config.trailing.enabled:
                self._check_trailing(pos, ctx, last_price)

    def cancel_stale_entries(self, timeout_seconds: float) -> None:
        """Cancels entry orders still working `timeout_seconds` after they
        were submitted.

        Every entry is a DAY limit order priced at the close of the bar that
        triggered it, so anything that doesn't fill promptly just rests at
        the broker until EOD -- and can fill hours later, on a completely
        different tape, still carrying the stop and target computed from the
        original setup's structure. Confirmed live: RLGT's 2026-09-15
        vwap_reversion entry signalled at 04:30 ET and filled at 08:26 ET,
        3h56m later.

        Shares already filled are left alone -- they are a real position and
        keep their resting stop. Only the unfilled remainder is cancelled,
        and `parent_done` is set so the rest of this class stops waiting for
        fills that are never coming (see _close_out)."""
        if timeout_seconds <= 0:
            return
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=timeout_seconds)
        for lots in list(self._positions.values()):
            for pos in list(lots):
                if pos.parent_done or pos.submitted_at > cutoff:
                    continue
                logger.warning(
                    "Entry for %s still working %.0fs after submission -- cancelling the unfilled "
                    "remainder (filled so far: %d shares)",
                    pos.symbol,
                    timeout_seconds,
                    pos.remaining_qty,
                )
                self.ib.cancelOrder(pos.parent_order)
                pos.parent_done = True
                if not pos.entry_filled:
                    # Nothing ever filled, so there is no position to
                    # protect and no exit leg worth keeping: IBKR cancels
                    # the attached children along with their parent.
                    self._untrack(pos)

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
        if pos.resize_task is not None:
            pos.resize_task.cancel()
            pos.resize_task = None
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

    def _schedule_stop_resize(self, pos: ManagedPosition) -> None:
        """Debounced entry point for quantity-only stop resizes (fill-driven,
        as opposed to the price-driven breakeven/trailing calls, which stay
        immediate since on_bar already rate-limits those to once per bar).
        Cancelling and rescheduling on every call means a burst of fills
        collapses into a single _replace_stop_order once the burst goes
        quiet for _STOP_RESIZE_DEBOUNCE_SECONDS, sized off whichever
        remaining_qty is current at that point -- never a stale snapshot
        from when the burst started."""
        if pos.resize_task is not None:
            pos.resize_task.cancel()

        async def _debounced() -> None:
            try:
                await asyncio.sleep(_STOP_RESIZE_DEBOUNCE_SECONDS)
            except asyncio.CancelledError:
                return
            pos.resize_task = None
            self._replace_stop_order(pos, new_qty=pos.remaining_qty)

        pos.resize_task = asyncio.ensure_future(_debounced())

    def _replace_stop_order(
        self, pos: ManagedPosition, new_price: float | None = None, new_qty: int | None = None
    ) -> None:
        """Cancel-and-replace instead of in-place modification, for both
        price moves (breakeven/trailing) and quantity changes (a fill on
        the entry or a target leg). IBKR rejects in-place modification of
        an order that's OCA-grouped or already been (partially) filled
        with error 10326 ("OCA group revision is not allowed") -- and
        ib_async marks the trade locally Cancelled on that error even
        though it may still be live at the broker, silently killing
        protection on the position. A fresh order with a fresh orderId
        sidesteps this entirely -- including when the "old" order is
        already Cancelled (harmless no-op to cancel it again), which
        happens whenever this runs right after a temporary flat-for-now
        state (see _close_out's parent_done branch).

        If `new_qty` resolves to <= 0, there's nothing to protect right
        now -- just cancel the old order and leave the position without a
        resting stop rather than submitting an invalid zero-quantity
        order; the next real fill (see on_entry_fill) calls back in here
        to establish a fresh one."""
        old_order = pos.stop_order
        price = round_to_tick(new_price) if new_price is not None else pos.current_stop_price
        qty = pos.remaining_qty if new_qty is None else new_qty
        if qty <= 0:
            self.ib.cancelOrder(old_order)
            return
        limit_price = None
        if getattr(old_order, "orderType", None) == "STP LMT":
            # keep the limit offset in the same direction bracket_builder
            # used when the order was first built, so trailing/breakeven
            # moves don't drift the limit's protective distance
            if old_order.action == "SELL":
                limit_price = round_to_tick(price * (1 - self.stop_limit_offset_pct / 100.0))
            else:
                limit_price = round_to_tick(price * (1 + self.stop_limit_offset_pct / 100.0))
            new_order = StopLimitOrder(
                old_order.action,
                qty,
                lmtPrice=limit_price,
                stopPrice=price,
                orderId=self.ib.client.getReqId(),
                # No parentId here (unlike the original bracket leg this is
                # replacing): _replace_stop_order only ever runs once
                # entry_filled is true (see on_bar's gate), so the entry
                # order this would reference is already Filled and no
                # longer open at the broker -- IBKR can't resolve a
                # parentId against a closed order and rejects the whole
                # submission ("Can't find order with id = <parent>"),
                # which silently leaves the position with no resting stop
                # at all. entry_filled is the actual safety net here.
                transmit=True,
                outsideRth=True,
                tif="DAY",
            )
        else:
            new_order = StopOrder(
                old_order.action,
                qty,
                price,
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
            stop_price=price,
            oca_group=new_order.ocaGroup or None,
            status=new_trade.orderStatus.status,
        )
        self.journal.update_order_status(pos.stop_row_id, "Cancelled")

        pos.stop_order = new_order
        pos.stop_row_id = new_row_id
        pos.current_stop_price = price
        self._wire_stop_fill(pos, new_trade)

    def _on_target_fill(self, pos: ManagedPosition, fill) -> None:
        filled_qty = fill.execution.shares
        pos.remaining_qty = max(0, pos.remaining_qty - filled_qty)
        if pos.remaining_qty <= 0:
            self._close_out(pos, cancel_stop=True, cancel_target=False)
            return
        # Resize the stop down after ANY partial target fill (not just a
        # scale_out tier) -- shares that already left via a target fill
        # must not stay covered by a stop still sized for the full lot.
        self._schedule_stop_resize(pos)
        logger.info("Target fill for %s: stop resize scheduled for %d shares", pos.symbol, pos.remaining_qty)

    def _on_stop_fill(self, pos: ManagedPosition, trade: Trade, fill, journal_fill: bool = True) -> None:
        if journal_fill:
            commission = None
            realized_pnl = None
            if fill.commissionReport is not None:
                commission = fill.commissionReport.commission
                # UNSET_DOUBLE sentinel on the opening leg of a round trip; see account_state.py
                pnl = fill.commissionReport.realizedPNL
                if pnl is not None and abs(pnl) < 1e15:
                    realized_pnl = pnl
            self.journal.record_fill(
                order_row_id=pos.stop_row_id,
                ib_order_id=trade.order.orderId,
                fill_qty=fill.execution.shares,
                fill_price=fill.execution.price,
                commission=commission,
                realized_pnl=realized_pnl,
            )
            # journal_fill=True means this stop was cancel-and-replaced at
            # least once (breakeven/trailing) -- track()'s ORIGINAL stop is
            # the only one OrderManager._attach_tracking ever sees, so its
            # on_fill is the sole place a stop-loss fill normally reaches
            # Discord. Every replacement stop's Trade object is wired only
            # here, in PositionManager, which never sent anything to
            # Discord at all -- a stop-loss hit on a position that had
            # already moved to breakeven or was trailing (i.e. most winning
            # or scratched trades) was silently invisible in trade_activity,
            # journaled but never notified.
            self._notify_stop_fill(pos, fill, commission)

        filled_qty = fill.execution.shares
        pos.remaining_qty = max(0, pos.remaining_qty - filled_qty)
        if pos.remaining_qty <= 0:
            self._close_out(pos, cancel_stop=False, cancel_target=True)

    def _notify_stop_fill(self, pos: ManagedPosition, fill, commission: float | None) -> None:
        """Raw trade_activity line for a fill on a replaced stop -- same
        message shape and same (exit - entry) * shares P&L computation
        OrderManager.on_fill already uses for every other exit fill, so
        the channel reads consistently regardless of which class happened
        to be holding the listener when the order filled."""
        if not (self.notifications_config.enabled and self.notifications_config.notify_on_fill):
            return
        commission_cost = commission if commission is not None and abs(commission) < 1e15 else 0.0
        trade_pnl = (fill.execution.price - pos.signal.entry_price) * fill.execution.shares - commission_cost
        send_discord_message(
            f"💰 SELL {pos.symbol} {fill.execution.shares:g} @ ${fill.execution.price:.2f} "
            f"(P&L ${trade_pnl:.2f})",
            channel="trade_activity",
        )

    def _close_out(self, pos: ManagedPosition, cancel_stop: bool, cancel_target: bool) -> None:
        """Everything bought so far has also been sold -- best-effort
        cancel whichever counterpart order(s) are still resting (a no-op
        if IBKR's own OCA link already cancelled one).

        Only actually stops tracking the lot if the parent has no shares
        left to fill (pos.parent_done). Otherwise this is a real but
        temporary flat: the still-working parent can go on to fill more
        later (a fast tier fill closing out a small initial partial fill,
        exactly like the rest still arrives afterward), and untracking
        here would leave any such later fill with no listener left to
        protect it -- confirmed live (BLSG, 2026-09-14): 959 shares ended
        up with no resting stop this way. Staying tracked costs nothing:
        on_entry_fill re-establishes a fresh stop the moment more shares
        actually arrive, and on_bar's entry_filled gate already skips a
        lot with nothing currently held."""
        if cancel_stop:
            self.ib.cancelOrder(pos.stop_order)
        if cancel_target:
            for target_order in pos.target_orders:
                self.ib.cancelOrder(target_order)
        if pos.parent_done:
            if pos.resize_task is not None:
                pos.resize_task.cancel()
                pos.resize_task = None
            self._untrack(pos)
        else:
            logger.info(
                "%s flat for now but parent order still filling -- staying tracked so any further fills stay protected",
                pos.symbol,
            )
