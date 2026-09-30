from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from ib_async import IB, Contract, Order, StopLimitOrder, StopOrder, Trade

from warrior_bot.config import ExitsConfig, NotificationsConfig
from warrior_bot.logging_setup import alert
from warrior_bot.notify.discord import build_pnl_message, send_discord_message
from warrior_bot.persistence.journal import Journal, _iso
from warrior_bot.risk.account_state import AccountState
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
from warrior_bot.utils.panic import flatten_position
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
    # Count of "scale_out"-role target fills seen so far -- what
    # _check_breakeven gates on for a lot with configured profit tiers (see
    # its docstring), instead of racing an independent R-multiple trigger
    # against the same tier fill. Irrelevant (never incremented, never read)
    # for the single-fallback "target" role case.
    tier_fill_count: int = 0
    breakeven_done: bool = False
    trailing_active: bool = False
    # When the bracket was submitted -- the clock cancel_stale_entries runs
    # against, so a working entry can't outlive the setup that justified it.
    submitted_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # Pending debounced stop-resize (see _schedule_stop_resize) -- cancelled
    # and rescheduled on every fill so a burst only replaces the stop once,
    # after fills stop arriving for _STOP_RESIZE_DEBOUNCE_SECONDS.
    resize_task: asyncio.Task | None = None
    # True from the moment a replacement stop is placed until IBKR
    # acknowledges it (status leaves PendingSubmit). While set, a second
    # cancel-and-replace must NOT be issued: ib_async's cancelOrder only
    # sends the cancel and locally marks it PendingCancel, and IBKR
    # routinely refuses to cancel an order it has not yet acknowledged --
    # so replacing again here is how one position ends up with two live
    # full-size stops, the mechanism behind the 2026-09-16 NRXS naked
    # short. A request arriving while this is set is parked in
    # pending_stop_request and applied once the in-flight one settles.
    stop_replace_pending: bool = False
    pending_stop_request: tuple[float | None, int | None] | None = None


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
        account_state: AccountState | None = None,
    ):
        self.ib = ib
        self.journal = journal
        self.config = config
        self.stop_limit_offset_pct = stop_limit_offset_pct
        self.notifications_config = notifications_config or NotificationsConfig()
        # Only for the pnl channel's running daily-total line (see
        # _notify_stop_fill) -- never used for any trading decision, so a
        # missing value here just means that one line falls back to this
        # single fill's own P&L instead of the account's running total.
        self.account_state = account_state
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
        for pos in self._positions.get(symbol, []):
            self._cancel_resize_task(pos)
        self._positions.pop(symbol, None)

    def has_unfilled_entry(self, symbol: str) -> bool:
        """True while any lot for `symbol` still has a working entry order.

        The reconciliation watchdog drops symbols IBKR reports no position
        in -- correct for genuinely stale local state, catastrophic for a
        bracket whose limit entry simply hasn't filled yet. track() registers
        a lot the instant the bracket is submitted with remaining_qty=0, and
        entry_fill_timeout_seconds is 300s by default (RLGT filled 3h56m late
        on 2026-09-15), so any entry slower than one 30s watchdog cycle used
        to be silently untracked. That lot then keeps its stop but loses
        breakeven, trailing and reversal-exit permanently -- on_bar can no
        longer see it -- and stops counting toward max_concurrent_positions,
        the 2-lot cap and the cross-strategy gate."""
        return any(not pos.parent_done for pos in self._positions.get(symbol, []))

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

        for target_trade, role in zip(target_trades, target_roles):
            self._wire_target_fill(pos, target_trade, role)
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

    def _wire_stop_fill(
        self, pos: ManagedPosition, trade: Trade, journal_fill: bool = True, row_id: int | None = None
    ) -> None:
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
        passes journal_fill=False to avoid double-recording it.

        `row_id` binds this listener to the journal row for THIS order,
        captured at wire time. Previously every listener read
        `pos.stop_row_id` at fire time, so a fill landing on a superseded
        stop (a cancel IBKR refused, or one that filled in the
        cancel-in-flight window) was journaled against the REPLACEMENT's
        row. Deliberately not detached on replacement: if the cancel didn't
        actually land and the old stop really fills, those shares really
        did sell, and dropping the event would leave remaining_qty
        overstating the position with nothing to notice it until the
        reconciliation watchdog's next pass."""
        bound_row_id = row_id if row_id is not None else pos.stop_row_id

        def on_stop_fill(t: Trade, fill) -> None:
            self._on_stop_fill(pos, t, fill, journal_fill=journal_fill, row_id=bound_row_id)

        trade.fillEvent += on_stop_fill

    def _wire_target_fill(self, pos: ManagedPosition, trade: Trade, role: str = "target") -> None:
        trade.fillEvent += lambda t, fill: self._on_target_fill(pos, fill, role)

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
                # Gate on parent_done, NOT entry_filled. A lot that filled
                # 300 of 2,000 shares before the disconnect has
                # entry_filled=True but parent_done=False -- its parent is
                # still working, and without a re-wired listener the
                # remaining 1,700 shares fill with no remaining_qty
                # increment and no stop resize, leaving the resting stop
                # sized for 300 while 2,000 are held. parent_done also
                # never flips, so the lot is never untracked and
                # cancel_stale_entries skips it forever.
                if not pos.parent_done:
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

                for target_order, role in zip(pos.target_orders, pos.target_roles):
                    fresh_target = open_by_id.get(target_order.orderId)
                    if fresh_target is not None:
                        self._wire_target_fill(pos, fresh_target, role)
                        # Deliberately NOT claimed. Claiming told
                        # OrderManager.resync_open_orders to skip re-attaching
                        # its own listener, but _on_target_fill journals
                        # nothing and notifies nothing -- so every trim fill
                        # after a reconnect vanished from the journal and
                        # from Discord. OrderManager owns journaling target
                        # fills; this class only tracks quantity off them.
                    else:
                        logger.warning(
                            "Resync: target order for %s (orderId=%d, role=%s) not found after reconnect -- "
                            "it may have filled while disconnected; remaining_qty may overstate the position",
                            symbol,
                            target_order.orderId,
                            role,
                        )

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

            # Per-lot isolation: a failure managing one lot (a disconnect
            # mid-replacement, an IBKR rejection) must not abort the whole
            # bar. Unhandled, it propagated out of on_bar and skipped every
            # remaining lot AND every strategy evaluation for that bar,
            # since _on_new_bar calls this before the strategy loop.
            try:
                if self.config.reversal_exit.enabled and self._check_reversal_exit(pos, ctx):
                    continue  # this lot is now flat -- evaluate any remaining lot for this symbol

                if not pos.breakeven_done and self.config.breakeven.enabled:
                    self._check_breakeven(pos, last_price)

                if pos.breakeven_done and self.config.trailing.enabled:
                    self._check_trailing(pos, ctx, last_price)
            except Exception:
                logger.exception("Position management failed for %s -- continuing with other lots", pos.symbol)
                alert(
                    f"Position management FAILED for {pos.symbol} -- stop may not have moved, check IBKR",
                    channel="kill_switch",
                )

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
        for lots in self._positions.values():
            for pos in lots:
                self._cancel_resize_task(pos)
        self._positions.clear()

    def _cancel_resize_task(self, pos: ManagedPosition) -> None:
        """Kills any armed stop-resize before a lot stops being tracked.

        Without this a resize armed moments before a flatten fires ~1.5s
        LATER and places a brand-new full-size protective stop on a
        position that no longer exists -- after panic_stop's global cancel
        has already swept -- where it rests for the day and can fill into
        an unintended short. _reversal_exit and _close_out always cancelled
        it; clear() and drop_symbol(), i.e. both flatten paths, did not."""
        if pos.resize_task is not None:
            pos.resize_task.cancel()
            pos.resize_task = None

    def _untrack(self, pos: ManagedPosition) -> None:
        self._cancel_resize_task(pos)
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
        """Ross's rule sequences these two actions: sell (part of) the
        position at the first profit target, THEN move the stop to
        breakeven as a consequence of having banked that profit -- not two
        independent triggers racing the same R-multiple (which is what an
        R-multiple-only check does: it used to fire at 0.5R regardless of
        whether the 1.0R tier had actually filled yet, the opposite order
        from Ross's rule). For a lot with a "scale_out"-role tier configured,
        this now waits for that tier's fill event specifically; only the
        tiers-disabled fallback ("target"-role only) still uses the
        independent trigger_r_multiple race, since there's no tier fill to
        sequence after in that case."""
        if "scale_out" in pos.target_roles:
            if pos.tier_fill_count < 1:
                return
        elif self._current_r(pos, last_price) < self.config.breakeven.trigger_r_multiple:
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

        # Move the stop FIRST, and only cancel the static target / latch
        # trailing_active once that succeeded. The old order did the
        # opposite: it cancelled the target and set trailing_active before
        # calling _replace_stop_order, so if that raised (a disconnect makes
        # getReqId() raise ConnectionError, and placeOrder can fail outright)
        # the position was left with neither a target nor a moved stop, and
        # just_activated could never become True again to retry.
        try:
            self._replace_stop_order(pos, new_stop)
        except Exception:
            logger.exception("Trailing stop replacement failed for %s -- leaving target in place", pos.symbol)
            return

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
        self._cancel_resize_task(pos)
        self.ib.cancelOrder(pos.stop_order)
        for target_order in pos.target_orders:
            self.ib.cancelOrder(target_order)
        # Was a bare MarketOrder with no outsideRth handling -- IBKR ignores
        # outsideRth on a market order and just queues it until 09:30 (the
        # same plumbing bug already fixed everywhere else this bot force-
        # closes a position; see panic.py's own docstring), which is exactly
        # backwards for an exit meant to happen NOW. flatten_position
        # already does the right thing: a market order in regular hours, a
        # marketable limit outside them. This is also why reversal_exit
        # stayed disabled in config until now (docs/strategy_decisions.md,
        # "Deferred: marketable-limit conversion...") -- the signals
        # themselves were never in question, only this order-placement gap.
        # The exit IS journaled now. Until 2026-09-30 this passed no
        # on_order_placed hook at all, so a reversal exit produced no orders
        # row and no fills row -- the trade stayed "open, $0 realized"
        # forever in dashboard_report.py and was silently dropped entirely by
        # win_rate_analysis.py. Confirmed live: LABT (2026-09-28) plus BKYI,
        # CNTB and VBIO this week all read as open positions after they had
        # demonstrably been flattened. Pointedly, reversal_exit was enabled
        # to attack the 120min+ duration bucket, and every trade it produced
        # was invisible to the duration report.
        flatten_position(
            self.ib,
            SimpleNamespace(contract=pos.contract, position=pos.remaining_qty),
            channel="limits",
            on_order_placed=lambda symbol, trade, order: self._journal_reversal_exit_fill(
                symbol, trade, order, signal_id=pos.signal_id
            ),
        )
        reason_str = ",".join(reasons)
        logger.warning("Reversal exit for %s: %s (qty=%d)", pos.symbol, reason_str, pos.remaining_qty)
        self.journal.record_kill_switch_event(
            triggered_by=f"reversal_exit:{pos.symbol}:{reason_str}", action_taken="market_exit_position"
        )
        self._untrack(pos)

    def _journal_reversal_exit_fill(
        self, symbol: str, trade: Trade, order: Order, signal_id: int | None = None
    ) -> None:
        """Records a reversal exit's order and fill against its own lot.

        Simpler than main._journal_flatten_fill's proportional split: a
        reversal exit is issued for one specific lot, so the signal_id is
        known and there is nothing to apportion. It is passed in rather than
        looked up, because the caller untracks the lot immediately after --
        a lookup here would race that and silently lose the attribution."""
        try:
            row_id = self.journal.record_order(
                signal_id=signal_id,
                symbol=symbol,
                ib_order_id=order.orderId,
                role="reversal_exit",
                action=order.action,
                qty=order.totalQuantity,
                order_type=order.orderType,
                limit_price=getattr(order, "lmtPrice", None),
                stop_price=None,
                oca_group=None,
                status=trade.orderStatus.status,
            )
        except Exception:
            logger.exception("Could not journal reversal-exit order for %s", symbol)
            return

        def on_fill(t: Trade, fill) -> None:
            try:
                self.journal.record_fill(
                    order_row_id=row_id,
                    ib_order_id=order.orderId,
                    fill_qty=fill.execution.shares,
                    fill_price=fill.execution.price,
                    exec_id=getattr(fill.execution, "execId", None),
                    exec_ts=_iso(getattr(fill.execution, "time", None)),
                )
            except Exception:
                logger.exception("Could not journal reversal-exit fill for %s", symbol)

        trade.fillEvent += on_fill

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
            # Anything other than CancelledError used to escape into an
            # un-awaited task and surface only as an unretrieved-exception
            # message at GC time -- i.e. the stop silently never got
            # resized. A failed resize is a protection failure, not a
            # logging event.
            try:
                self._replace_stop_order(pos, new_qty=pos.remaining_qty)
            except Exception:
                logger.exception("Debounced stop resize failed for %s", pos.symbol)
                alert(
                    f"Stop resize FAILED for {pos.symbol} ({pos.remaining_qty} shares) -- "
                    "position may be under-protected, check IBKR",
                    channel="kill_switch",
                )

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
        to establish a fresh one.

        Serialized via pos.stop_replace_pending: a replacement requested
        while a previous one is still unacknowledged is parked and applied
        when that one settles, rather than issuing a second
        cancel-and-replace. See ManagedPosition.stop_replace_pending for
        why doing otherwise can leave two live stops on one position."""
        if pos.stop_replace_pending:
            # Merge with anything already parked -- last writer wins per
            # field, so a price move and a size change both survive.
            parked_price, parked_qty = pos.pending_stop_request or (None, None)
            pos.pending_stop_request = (
                new_price if new_price is not None else parked_price,
                new_qty if new_qty is not None else parked_qty,
            )
            logger.info(
                "Stop replacement for %s deferred -- previous replacement not acknowledged yet", pos.symbol
            )
            return

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
            symbol=pos.symbol,
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
        self._wire_stop_fill(pos, new_trade, row_id=new_row_id)
        self._wire_stop_status(pos, new_trade, new_row_id)

    # IBKR has not acknowledged an order in these states, so a cancel for it
    # may well be refused -- see ManagedPosition.stop_replace_pending.
    _UNACKNOWLEDGED_STATES = frozenset({"PendingSubmit", "ApiPending"})

    def _wire_stop_status(self, pos: ManagedPosition, trade: Trade, row_id: int) -> None:
        """Tracks a replacement stop's status. PositionManager previously
        wired no statusEvent at all (only OrderManager._attach_tracking
        does, and it never sees a replacement stop), which left every
        replacement's journal row frozen at PendingSubmit forever -- 227 of
        them across the journal -- and left this class with no way to know
        whether a cancel-and-replace had actually been acknowledged."""
        pos.stop_replace_pending = trade.orderStatus.status in self._UNACKNOWLEDGED_STATES

        def on_status(t: Trade) -> None:
            status = t.orderStatus.status
            self.journal.update_order_status(row_id, status)
            if status in self._UNACKNOWLEDGED_STATES or pos.stop_order is not t.order:
                return
            pos.stop_replace_pending = False
            parked = pos.pending_stop_request
            if parked is not None:
                pos.pending_stop_request = None
                price, qty = parked
                logger.info("Applying deferred stop replacement for %s", pos.symbol)
                self._replace_stop_order(pos, new_price=price, new_qty=qty)

        trade.statusEvent += on_status

    def _apply_exit_fill(self, pos: ManagedPosition, filled_qty: float) -> None:
        """Decrements remaining_qty, and treats an oversell as the incident
        it is rather than clamping it away.

        Profit tiers are deliberately not OCA-linked to the stop
        (bracket_builder), so both legs stay independently live and can fill
        nearly simultaneously. `max(0, ...)` used to silently erase the
        evidence, leaving a real short at IBKR that only the reconciliation
        watchdog would notice, up to 30s later."""
        remaining = pos.remaining_qty - int(round(filled_qty))
        if remaining < 0:
            logger.error(
                "OVERSELL on %s: exits exceeded the position by %d shares -- flattening immediately",
                pos.symbol,
                -remaining,
            )
            alert(
                f"OVERSELL on {pos.symbol}: sold {-remaining} shares more than held "
                "(tier and stop likely filled together) -- flattening now",
                channel="kill_switch",
            )
            flatten_position(
                self.ib, SimpleNamespace(contract=pos.contract, position=remaining), channel="kill_switch"
            )
        pos.remaining_qty = max(0, remaining)

    def _on_target_fill(self, pos: ManagedPosition, fill, role: str = "target") -> None:
        self._apply_exit_fill(pos, fill.execution.shares)
        if role == "scale_out":
            pos.tier_fill_count += 1
        if pos.remaining_qty <= 0:
            self._close_out(pos, cancel_stop=True, cancel_target=False)
            return
        # Resize the stop down after ANY partial target fill (not just a
        # scale_out tier) -- shares that already left via a target fill
        # must not stay covered by a stop still sized for the full lot.
        #
        # Synchronous, NOT debounced. The debounce exists to coalesce entry
        # fill bursts, where the error direction is harmless (a stop briefly
        # sized for fewer shares than held). Downsizing is the dangerous
        # direction: for the whole debounce window the resting stop still
        # covers shares that have already been sold, so a flush through it
        # sells them twice and opens a real short.
        self._replace_stop_order(pos, new_qty=pos.remaining_qty)
        logger.info("Target fill for %s: stop resized to %d shares", pos.symbol, pos.remaining_qty)

    def _on_stop_fill(
        self, pos: ManagedPosition, trade: Trade, fill, journal_fill: bool = True, row_id: int | None = None
    ) -> None:
        # Bound at wire time -- see _wire_stop_fill. Falls back to the
        # current row only for callers that predate the binding.
        target_row_id = row_id if row_id is not None else pos.stop_row_id
        if journal_fill:
            # commission/realized_pnl deliberately omitted: ib_async emits
            # fillEvent with an empty CommissionReport and sends the real
            # values separately, keyed on execId. They arrive on the row via
            # OrderManager._on_commission_report.
            self.journal.record_fill(
                order_row_id=target_row_id,
                ib_order_id=trade.order.orderId,
                fill_qty=fill.execution.shares,
                fill_price=fill.execution.price,
                exec_id=getattr(fill.execution, "execId", None),
                exec_ts=_iso(getattr(fill.execution, "time", None)),
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
            self._notify_stop_fill(pos, fill)

        self._apply_exit_fill(pos, fill.execution.shares)
        if pos.remaining_qty <= 0:
            self._close_out(pos, cancel_stop=False, cancel_target=True)

    def _notify_stop_fill(self, pos: ManagedPosition, fill) -> None:
        """trade_activity line + pnl channel message for a fill on a
        replaced stop -- same message shapes and same (exit - entry) *
        shares P&L computation OrderManager.on_fill already uses for every
        other exit fill, so both channels read consistently regardless of
        which class happened to be holding the listener when the order
        filled. Confirmed live, 2026-09-24: this method originally only
        sent the trade_activity line (2026-09-23 fix) -- the pnl channel
        stayed just as silent on a replaced stop's fill as it was before
        that fix, since nothing here ever called build_pnl_message."""
        # Gross of commission -- it is not known at fill time (see
        # record_fill), and the net figure lands on the journal row shortly
        # after via OrderManager._on_commission_report.
        trade_pnl = (fill.execution.price - pos.signal.entry_price) * fill.execution.shares

        if self.notifications_config.enabled and self.notifications_config.notify_on_fill:
            send_discord_message(
                f"💰 SELL {pos.symbol} {fill.execution.shares:g} @ ${fill.execution.price:.2f} "
                f"(P&L ${trade_pnl:.2f})",
                channel="trade_activity",
            )

        if self.notifications_config.enabled and self.notifications_config.notify_on_pnl:
            daily_pnl = self.account_state.snapshot().daily_realized_pnl if self.account_state else trade_pnl
            send_discord_message(build_pnl_message(pos.symbol, trade_pnl, daily_pnl), channel="pnl")

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
            self._untrack(pos)
        else:
            logger.info(
                "%s flat for now but parent order still filling -- staying tracked so any further fills stay protected",
                pos.symbol,
            )
