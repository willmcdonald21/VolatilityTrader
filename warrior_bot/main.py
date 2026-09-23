from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone

from ib_async import Contract, Order, Trade

from warrior_bot.broker.historical import fetch_prior_close, fetch_warmup_bars
from warrior_bot.broker.ib_client import IBClient
from warrior_bot.broker.market_data import scan_candidates
from warrior_bot.broker.news import discover_provider_codes, fetch_recent_headlines
from warrior_bot.config import AppConfig, load_config
from warrior_bot.execution.order_manager import OrderManager
from warrior_bot.execution.position_manager import PositionManager
from warrior_bot.logging_setup import alert, setup_logging
from warrior_bot.notify.discord import send_discord_message
from warrior_bot.persistence.db import get_connection
from warrior_bot.persistence.journal import Journal
from warrior_bot.risk.account_state import AccountState
from warrior_bot.risk.risk_manager import RiskManager
from warrior_bot.scanner.catalyst import classify_headlines
from warrior_bot.scanner.float_provider import FloatProvider
from warrior_bot.scanner.regime import count_extreme_gainers
from warrior_bot.signals.signal import Signal
from warrior_bot.strategies.abcd_pattern import AbcdStrategy
from warrior_bot.strategies.base_strategy import BaseStrategy, SymbolContext
from warrior_bot.strategies.bull_flag import BullFlagStrategy
from warrior_bot.strategies.gap_and_go import GapAndGoStrategy
from warrior_bot.strategies.indicators import Bar
from warrior_bot.strategies.inverted_head_and_shoulders import InvertedHeadAndShouldersStrategy
from warrior_bot.strategies.vwap_reversion import VwapReversionStrategy
from warrior_bot.utils.panic import flatten_position, panic_stop
from warrior_bot.utils.rounding import round_to_tick
from warrior_bot.utils.time_utils import is_active_session, to_eastern

logger = logging.getLogger("warrior_bot.main")


def _bar_from_ib(b) -> Bar:
    return Bar(time=b.date, open=b.open, high=b.high, low=b.low, close=b.close, volume=b.volume)


class WarriorBot:
    def __init__(self, config: AppConfig, dry_run: bool = False):
        self.config = config
        self.dry_run = dry_run
        self.logger = setup_logging(config)
        self.ib_client = IBClient(config)
        self.ib = self.ib_client.ib

        self.contexts: dict[str, SymbolContext] = {}
        self.contracts: dict[str, Contract] = {}
        self._subscriptions: dict[str, object] = {}
        # Wall-clock time each symbol's live bar callback last fired (see
        # _make_bar_update_handler) -- the sole signal the data watchdog
        # (_check_stale_subscriptions) uses to detect a feed that's gone
        # silent, whatever the underlying cause.
        self._last_bar_at: dict[str, datetime] = {}
        # Wall-clock time each symbol was last present in the scanner's
        # top-N results (stamped once per _scan_loop tick, for every
        # candidate it returns) -- protects currently-relevant symbols from
        # eviction and picks the least-relevant one when capacity is needed.
        self._last_scan_seen_at: dict[str, datetime] = {}
        self.ib.connectedEvent += self._on_connected

        conn = get_connection(config.resolve_path(config.journal.db_path))
        self.journal = Journal(conn)
        self.account_state = AccountState(self.ib)
        self.position_manager = PositionManager(
            self.ib,
            self.journal,
            config.exits,
            stop_limit_offset_pct=config.execution.stop_limit_offset_pct,
            notifications_config=config.notifications,
        )
        self.risk_manager = RiskManager(
            config.risk,
            self.account_state,
            self.position_manager,
            config.resolve_path(config.kill_switch.flag_file),
            no_entry_after_et=config.exits.eod_flatten_time,
        )
        self.order_manager = OrderManager(
            self.ib,
            self.journal,
            config.exits,
            self.position_manager,
            execution_config=config.execution,
            notifications_config=config.notifications,
            account_state=self.account_state,
            trading_mode=config.trading.mode,
        )

        float_provider = FloatProvider(config.resolve_path("config/float_list.csv"))

        self.strategies: list[BaseStrategy] = []
        if config.strategies.gap_and_go.enabled:
            self.strategies.append(GapAndGoStrategy(config.strategies.gap_and_go, float_provider=float_provider))
        if config.strategies.bull_flag.enabled:
            self.strategies.append(BullFlagStrategy(config.strategies.bull_flag, config.pullback_quality))
        if config.strategies.abcd.enabled:
            self.strategies.append(AbcdStrategy(config.strategies.abcd, config.pullback_quality))
        if config.strategies.vwap_reversion.enabled:
            self.strategies.append(VwapReversionStrategy(config.strategies.vwap_reversion))
        if config.strategies.inverted_head_and_shoulders.enabled:
            self.strategies.append(
                InvertedHeadAndShouldersStrategy(config.strategies.inverted_head_and_shoulders)
            )

        self._scan_task: asyncio.Task | None = None
        self._risk_task: asyncio.Task | None = None
        self._watchdog_task: asyncio.Task | None = None
        self._reconciliation_task: asyncio.Task | None = None
        # Tracked separately, not as one shared flag: a daily-loss-limit
        # flatten firing earlier in the day must never suppress the 15:55
        # EOD sweep later that same day. A single _flattened_today flag did
        # exactly that -- should_flatten_for_loss_limit re-derives from live
        # P&L on every check (not a one-way latch), so realized P&L can
        # recover, new entries resume, and anything opened after the early
        # loss-limit flatten had nothing left to close it if the EOD sweep
        # believed the day was already handled.
        self._eod_flatten_fired = False
        self._loss_limit_flatten_fired = False
        self._news_provider_codes = config.news.provider_codes
        self._last_logged_breadth: int | None = None
        # ET calendar date this process last reset daily state for. This is
        # what makes EOD flatten (and everything else in reset_daily_state)
        # keep working every day for a long-running process -- without it,
        # the flags above only ever get cleared once, in __init__, so a
        # process that stays up past midnight silently stops flattening
        # forever after its first day (see _check_new_trading_day).
        self._trading_day: date | None = None

    async def start(self) -> None:
        await self.ib_client.connect()
        self.order_manager.resync_open_orders()
        snapshot = self.account_state.snapshot()
        self.risk_manager.mark_start_of_day(snapshot.net_liquidation)
        self.journal.record_account_snapshot(snapshot)
        self._trading_day = to_eastern(datetime.now(timezone.utc)).date()
        if self.config.news.enabled and not self._news_provider_codes:
            try:
                self._news_provider_codes = await discover_provider_codes(self.ib)
            except Exception:
                self.logger.exception("Failed to discover news providers")
        self._scan_task = asyncio.ensure_future(self._scan_loop())
        self._risk_task = asyncio.ensure_future(self._risk_loop())
        self._watchdog_task = asyncio.ensure_future(self._data_watchdog_loop())
        self._reconciliation_task = asyncio.ensure_future(self._position_reconciliation_loop())
        self.logger.info(
            "WarriorBot started: mode=%s strategies=%s",
            self.config.trading.mode,
            [s.name for s in self.strategies],
        )

    async def stop(self) -> None:
        if self._scan_task:
            self._scan_task.cancel()
        if self._risk_task:
            self._risk_task.cancel()
        if self._watchdog_task:
            self._watchdog_task.cancel()
        if self._reconciliation_task:
            self._reconciliation_task.cancel()
        self.ib_client.disconnect()

    def _on_connected(self) -> None:
        """Fires on every successful (re)connect, including reconnects --
        ib_async's own disconnect() docs state it "will clear all session
        state", which silently kills every symbol's live bar subscription
        without raising anything. A no-op on the very first connect (there's
        nothing tracked yet); on a reconnect, drops already-tracked symbols
        so `_scan_loop` treats them as new again and re-onboards them --
        fresh contract qualification, warmup bars, and a live bar
        subscription on the new session.

        Also resyncs order/position fill tracking -- ib_async's
        disconnect() calls wrapper.reset(), which wipes its own internal
        Trade-object cache, so any fillEvent listener OrderManager or
        PositionManager wired onto a pre-reconnect Trade goes silently
        dead even though the underlying order keeps working fine at the
        broker. Confirmed live, 2026-09-16: exactly this silently orphaned
        PositionManager's tracking of two NRXS stop fills, leaving a
        closed lot looking open until a stale resize placed a duplicate
        full-size stop that later filled for real -- an 850-share naked
        short with no resting protection for ~3h53m. position_manager's
        resync runs first and reports which stop/target orderIds it
        re-wired itself, so order_manager's resync (force=True, since it
        would otherwise skip every orderId it already knows about from
        before the reconnect -- see resync_open_orders' docstring) doesn't
        also re-attach its own journaling listener to those and
        double-journal the next fill."""
        if not self.contexts:
            return
        orphaned = list(self.contexts.keys())
        self.logger.warning(
            "Reconnected -- dropping bar subscriptions for %d already-tracked symbol(s) "
            "(disconnect clears all session state); will re-onboard on next scan: %s",
            len(orphaned),
            orphaned,
        )
        self.contexts.clear()
        self.contracts.clear()
        self._subscriptions.clear()
        self._last_bar_at.clear()
        self._last_scan_seen_at.clear()

        claimed = self.position_manager.resync_after_reconnect(self.ib)
        self.order_manager.resync_open_orders(force=True, skip_order_ids=frozenset(claimed))

    async def _scan_loop(self) -> None:
        while True:
            if not self.ib.isConnected():
                await asyncio.sleep(5)
                continue
            try:
                symbols = await asyncio.wait_for(scan_candidates(self.ib, self.config), timeout=30)
                now = datetime.now(timezone.utc)
                for symbol in symbols:
                    # Stamped for every symbol still in the current top-N,
                    # already-tracked ones included -- this is what protects
                    # a still-relevant symbol from eviction in
                    # _pick_eviction_candidate below, and what gives a
                    # brand-new candidate an immediate grace period the
                    # moment it's about to be onboarded.
                    self._last_scan_seen_at[symbol] = now
                for rank, symbol in enumerate(symbols, start=1):
                    if symbol not in self.contexts:
                        await self._onboard_symbol(symbol, scanner_rank=rank)
                    else:
                        # Re-stamp the live rank on every tick. Rank is the
                        # bot's whole notion of "how obvious is this name
                        # right now", and RiskManager gates its reserved
                        # top-tier slot on it -- freezing it at whatever the
                        # symbol happened to rank when first onboarded means
                        # a name that opens mid-pack and later becomes THE
                        # leader of the day is still judged on its opening
                        # rank, and gets turned away from the slot that
                        # exists precisely for it.
                        self.contexts[symbol].scanner_rank = rank
                self._demote_symbols_absent_from_scan(set(symbols))
                breadth = count_extreme_gainers(self.contexts.values())
                if breadth != self._last_logged_breadth:
                    self.logger.info(
                        "Market breadth: %d onboarded symbol(s) gapped >=100%% -- regime signal only, not sized on",
                        breadth,
                    )
                    self._last_logged_breadth = breadth
            except Exception:
                self.logger.exception("Scan loop iteration failed")
            await asyncio.sleep(self.config.scanner.refresh_seconds)

    def _demote_symbols_absent_from_scan(self, current: set[str]) -> None:
        """A tracked symbol that has dropped out of the scanner's top-N is
        no longer a top-tier candidate, so it must not keep claiming a rank
        that says it is. Cleared to None rather than to a large number:
        RiskManager treats a missing rank as ineligible for the reserved
        slot, which is exactly right for a name that is no longer ranking."""
        for symbol, ctx in self.contexts.items():
            if symbol not in current and ctx.scanner_rank is not None:
                ctx.scanner_rank = None

    async def _risk_loop(self) -> None:
        while True:
            if not self.ib.isConnected():
                await asyncio.sleep(5)
                continue
            try:
                self._check_new_trading_day()
                self._check_flatten_triggers()
                self.position_manager.cancel_stale_entries(
                    self.config.risk.entry_fill_timeout_seconds
                )
            except Exception:
                self.logger.exception("Risk loop iteration failed")
            await asyncio.sleep(self.config.exits.risk_loop_interval_seconds)

    def _check_new_trading_day(self) -> None:
        """Runs reset_daily_state() the first time this loop notices the ET
        calendar date has changed, so day-trading discipline (EOD flatten,
        daily loss limit, starter-trade regime tracking) resets every day
        for a process that stays up past midnight -- not just once, on
        __init__, for whichever day the process happened to start on."""
        now_et_date = to_eastern(datetime.now(timezone.utc)).date()
        if now_et_date == self._trading_day:
            return
        snapshot = self.account_state.snapshot()
        if snapshot.open_positions_count > 0:
            # RiskManager's max_concurrent_positions check reads live IBKR
            # state, not PositionManager's local tracking, so clearing that
            # tracking below can never let a carried-over position get
            # ignored by risk gates -- but a real position surviving past
            # midnight means yesterday's EOD flatten didn't do its job, and
            # that's worth a loud, explicit alert rather than a silent reset.
            alert(
                f"New trading day starting with {snapshot.open_positions_count} position(s) still open "
                "-- the prior day's EOD flatten may have failed. This reset does NOT flatten them; "
                "check IBKR directly.",
                channel="limits",
            )
        self.reset_daily_state()
        self._trading_day = now_et_date

    def _check_flatten_triggers(self) -> None:
        now_et = to_eastern(datetime.now(timezone.utc))
        if not self._eod_flatten_fired and now_et.time() >= self.config.exits.eod_flatten_time:
            self._trigger_flatten("eod_flatten")
            self._eod_flatten_fired = True
            return
        if self._loss_limit_flatten_fired:
            return
        snapshot = self.account_state.snapshot()
        if self.risk_manager.should_flatten_for_loss_limit(snapshot):
            self._trigger_flatten("daily_loss_limit")
            self._loss_limit_flatten_fired = True

    def _trigger_flatten(self, reason: str) -> None:
        self.logger.warning("Flattening all positions: reason=%s", reason)
        alert(f"Flattening all positions and stopping for the day: reason={reason}", channel="limits")
        panic_stop(
            self.ib,
            flatten=True,
            channel="limits",
            limit_offset_pct=self.config.execution.flatten_limit_offset_pct,
            on_order_placed=self._journal_flatten_fill,
        )
        self.position_manager.clear()
        self.journal.record_kill_switch_event(triggered_by=reason, action_taken="cancel_all+flatten_all")

    def _journal_flatten_fill(self, symbol: str, trade: Trade, order: Order) -> None:
        """Wired as panic.py's on_order_placed hook for every emergency/EOD
        flatten order -- without this, that order's fill was completely
        invisible to the journal (no orders/fills row at all, since it
        bypasses OrderManager.submit_signal entirely), so any position
        force-closed by the reconciliation watchdog or a routine EOD
        flatten showed up in dashboard_report.py as "still open, $0
        realized" forever, no matter what it actually closed at --
        confirmed live 2026-09-23 while investigating a run of losing days,
        where this made the true damage from the 2026-09-21 premarket
        blowup unrecoverable after the fact.

        Pre-creates one `orders` row per currently-tracked lot for this
        symbol (there can be two, from a pyramid add-on), split by each
        lot's remaining_qty -- IBKR doesn't know about "lots", only a
        single aggregate position, so this is a best-effort proportional
        split, not a precise per-lot attribution. Good enough for P&L:
        dashboard_report.py sums by signal_id across all of a signal's
        fills regardless of which physical execution produced them.
        Silently skipped (logged) if PositionManager has no tracked lot for
        this symbol at all -- there's no signal_id to journal against
        (orders.signal_id is NOT NULL), e.g. a position the reconciliation
        watchdog already found completely orphaned."""
        lots = self.position_manager.lots_for_symbol(symbol)
        total_qty = sum(lot.remaining_qty for lot in lots)
        if not lots or total_qty <= 0:
            self.logger.warning(
                "Flatten fill for %s has no tracked lot to journal against -- its exit won't appear in dashboard P&L",
                symbol,
            )
            return

        row_shares = [
            (
                self.journal.record_order(
                    signal_id=lot.signal_id,
                    ib_order_id=order.orderId,
                    role="emergency_flatten",
                    action=order.action,
                    qty=round(order.totalQuantity * (lot.remaining_qty / total_qty), 4),
                    order_type=order.orderType,
                    limit_price=getattr(order, "lmtPrice", None),
                    stop_price=None,
                    oca_group=None,
                    status=trade.orderStatus.status,
                ),
                lot.remaining_qty / total_qty,
            )
            for lot in lots
        ]

        def on_fill(t, fill) -> None:
            commission = fill.commissionReport.commission if fill.commissionReport is not None else None
            for row_id, share in row_shares:
                self.journal.record_fill(
                    order_row_id=row_id,
                    ib_order_id=order.orderId,
                    fill_qty=fill.execution.shares * share,
                    fill_price=fill.execution.price,
                    commission=commission * share if commission is not None else None,
                    realized_pnl=None,
                )

        trade.fillEvent += on_fill

    async def _position_reconciliation_loop(self) -> None:
        cfg = self.config.position_reconciliation
        if not cfg.enabled:
            return
        while True:
            if not self.ib.isConnected():
                await asyncio.sleep(5)
                continue
            try:
                self._check_position_reconciliation()
            except Exception:
                self.logger.exception("Position reconciliation iteration failed")
            await asyncio.sleep(cfg.check_interval_seconds)

    def _check_position_reconciliation(self) -> None:
        """Unconditional backstop against the 2026-09-16 NRXS incident
        (unprotected 850-share naked short for ~3h53m, closed only by
        luck -- the scheduled EOD flatten happened to still be ahead of
        it): independent of *why* a symbol ends up here (a reconnect
        orphaning fill listeners, per PositionManager.resync_after_reconnect,
        or any other cause not yet discovered), this periodically checks
        IBKR's own live position/order state directly and immediately
        flattens any symbol holding a real position with no adequate
        resting protective stop. See PositionReconciliationConfig for the
        full incident writeup and the "flatten now" over "reconstruct the
        right stop" reasoning."""
        live_positions = {p.contract.symbol: p for p in self.ib.positions() if p.position != 0}

        # Stale local tracking: PositionManager thinks a symbol is still
        # open but IBKR shows it flat (e.g. a resync that couldn't resolve
        # a stop that filled/cancelled entirely while disconnected).
        for symbol in self.position_manager.tracked_symbols() - set(live_positions.keys()):
            self.logger.warning(
                "Reconciliation: %s tracked locally but flat at IBKR -- dropping stale local state", symbol
            )
            self.position_manager.drop_symbol(symbol)

        if not live_positions:
            return

        # Only a resting STP/STP LMT SELL order actually caps downside on
        # a long position -- a resting take-profit LMT order doesn't.
        stop_qty_by_symbol: dict[str, float] = {}
        for trade in self.ib.openTrades():
            if trade.order.action != "SELL" or trade.order.orderType not in ("STP", "STP LMT"):
                continue
            remaining = trade.orderStatus.remaining or trade.order.totalQuantity
            symbol = trade.contract.symbol
            stop_qty_by_symbol[symbol] = stop_qty_by_symbol.get(symbol, 0.0) + remaining

        for symbol, position in live_positions.items():
            if position.position < 0:
                # Never an intended state for this long-only bot (Signal.side
                # is always "BUY") -- any short found here IS the bug, by
                # definition, regardless of whether anything happens to
                # cover it.
                self._emergency_flatten_symbol(
                    symbol, position, reason="naked_short_detected", covered_qty=0.0
                )
                continue
            covered = stop_qty_by_symbol.get(symbol, 0.0)
            if covered < position.position:
                self._emergency_flatten_symbol(
                    symbol, position, reason="unprotected_position_detected", covered_qty=covered
                )

    def _emergency_flatten_symbol(self, symbol: str, position, reason: str, covered_qty: float) -> None:
        placed = flatten_position(
            self.ib,
            position,
            channel="limits",
            limit_offset_pct=self.config.execution.flatten_limit_offset_pct,
            on_order_placed=self._journal_flatten_fill,
        )
        if not placed:
            # A flatten for this symbol is already working -- nothing new to
            # alert on or journal, and re-alerting every check interval just
            # buries the real signal.
            return
        self.logger.error(
            "Reconciliation: %s has %s shares with only %.0f covered by a resting stop -- flattening immediately "
            "(reason=%s)",
            symbol,
            position.position,
            covered_qty,
            reason,
        )
        alert(
            f"Reconciliation watchdog: {symbol} found with a real position and no adequate resting "
            f"protective stop -- flattening immediately (reason={reason})",
            channel="limits",
        )
        self.position_manager.drop_symbol(symbol)
        self.journal.record_kill_switch_event(
            triggered_by=f"reconciliation:{symbol}:{reason}", action_taken="flatten_symbol"
        )

    def _ensure_subscription_capacity(self, incoming_symbol: str) -> bool:
        """Returns True once there's room for one more live subscription --
        evicting the least-relevant existing one first if already at the
        self-imposed cap. Returns False (onboarding should be skipped this
        tick, retried on a later scan) only when nothing is safely evictable
        right now -- e.g. every tracked symbol either holds an open position
        or was in the scanner's top-N recently. Never silently exceeds the
        cap; that's the whole point (see DataWatchdogConfig)."""
        cfg = self.config.data_watchdog
        if len(self._subscriptions) < cfg.max_concurrent_subscriptions:
            return True
        victim = self._pick_eviction_candidate(incoming_symbol)
        if victim is None:
            self.logger.warning(
                "At live-subscription cap (%d/%d) with no eligible symbol to free for %s -- "
                "skipping onboarding this scan tick",
                len(self._subscriptions),
                cfg.max_concurrent_subscriptions,
                incoming_symbol,
            )
            return False
        self._unsubscribe_symbol(victim, reason="capacity")
        return True

    def _pick_eviction_candidate(self, incoming_symbol: str) -> str | None:
        cfg = self.config.data_watchdog
        now = datetime.now(timezone.utc)
        threshold = timedelta(seconds=cfg.inactive_unsubscribe_seconds)
        epoch = datetime.min.replace(tzinfo=timezone.utc)
        eligible = [
            symbol
            for symbol in self._subscriptions
            if symbol != incoming_symbol
            and self.position_manager.open_lot_count(symbol) == 0
            and now - self._last_scan_seen_at.get(symbol, epoch) > threshold
        ]
        if not eligible:
            return None
        # Least recently relevant (oldest scanner appearance) goes first.
        return min(eligible, key=lambda s: self._last_scan_seen_at.get(s, epoch))

    def _unsubscribe_symbol(self, symbol: str, reason: str) -> None:
        keep_updated = self._subscriptions.pop(symbol, None)
        if keep_updated is not None:
            try:
                self.ib.cancelHistoricalData(keep_updated)
            except Exception:
                self.logger.exception("Failed to cancel live subscription for %s", symbol)
        self.contexts.pop(symbol, None)
        self.contracts.pop(symbol, None)
        self._last_bar_at.pop(symbol, None)
        self._last_scan_seen_at.pop(symbol, None)
        self.logger.info("Unsubscribed %s live bar feed (reason=%s)", symbol, reason)

    def _make_bar_update_handler(self, symbol: str, contract: Contract, ctx: SymbolContext):
        """Shared by both a fresh onboarding subscription and a watchdog
        resubscribe -- any callback firing at all (even a same-bar interim
        tick with has_new_bar=False) is proof the feed is alive, which is
        the only signal _check_stale_subscriptions has to go on. Wrapped in
        its own try/except: an uncaught exception here would otherwise be
        able to silently kill this listener inside eventkit with nothing
        in the log to explain the symbol going quiet -- exactly the
        failure mode under investigation, so this path doesn't get to be
        the one place in the callback chain without a safety net.

        bars[-2], not bars[-1], on a new-bar event: ib_async's own
        historicalDataUpdate (wrapper.py) appends a bar and sets
        has_new_bar=True the instant a new minute STARTS, with that new
        bar carrying only whatever trades have printed so far (often just
        one tick -- open==high==low==close). bars[-1] at that moment is
        that brand-new, still-forming bar; bars[-2] is the one that just
        finished and is the only one with a real, complete OHLCV. Every
        strategy reads ctx.bars[-1] as "the current/just-closed bar", and
        this array is never revisited after being appended (in-place
        updates to bars[-1] arrive with has_new_bar=False and are ignored
        above) -- feeding bars[-1] here permanently froze every bar in
        ctx.bars at its first-tick snapshot instead of its true range,
        corrupting entry price, breakout levels, ATR/EMA/VWAP, and
        relative volume alike. Confirmed against ib_async's wrapper.py
        (historicalDataUpdate: `if hasNewBar: bars.append(bar)`) and against
        warrior_bot/backtest/replay.py, which uses keepUpToDate=False and
        so only ever sees fully-closed bars -- the reason this was invisible
        to backtesting despite driving most of the strategy's quick,
        zero-favorable-excursion stop-outs live."""

        def on_update(bars, has_new_bar) -> None:
            self._last_bar_at[symbol] = datetime.now(timezone.utc)
            if not has_new_bar or len(bars) < 2:
                return
            try:
                ctx.add_bar(_bar_from_ib(bars[-2]))
                self._on_new_bar(contract, ctx)
            except Exception:
                self.logger.exception("Live bar callback failed for %s", symbol)

        return on_update

    async def _request_live_updates(self, contract: Contract):
        return await asyncio.wait_for(
            self.ib.reqHistoricalDataAsync(
                contract,
                endDateTime="",
                durationStr="3600 S",
                barSizeSetting="1 min",
                whatToShow="TRADES",
                useRTH=self.config.trading.use_rth,
                formatDate=2,
                keepUpToDate=True,
            ),
            timeout=30,
        )

    async def _onboard_symbol(self, symbol: str, scanner_rank: int | None = None) -> None:
        if not self._ensure_subscription_capacity(symbol):
            return
        try:
            contract = await self.ib_client.qualify_stock(symbol)
        except Exception:
            self.logger.exception("Could not qualify contract for %s", symbol)
            return

        ctx = SymbolContext(symbol=symbol, scanner_rank=scanner_rank)

        try:
            ctx.prior_close = await fetch_prior_close(self.ib, contract)
        except Exception:
            self.logger.exception("Failed to fetch prior close for %s", symbol)

        try:
            daily_bars = await asyncio.wait_for(
                self.ib.reqHistoricalDataAsync(
                    contract,
                    endDateTime="",
                    durationStr="20 D",
                    barSizeSetting="1 day",
                    whatToShow="TRADES",
                    useRTH=True,
                    formatDate=2,
                    keepUpToDate=False,
                ),
                timeout=30,
            )
            if daily_bars:
                ctx.avg_daily_volume = sum(b.volume for b in daily_bars) / len(daily_bars)
        except Exception:
            self.logger.exception("Failed to fetch avg daily volume for %s", symbol)

        try:
            warmup_bars = await fetch_warmup_bars(self.ib, contract, self.config)
            for b in warmup_bars:
                ctx.add_bar(_bar_from_ib(b))
        except Exception:
            self.logger.exception("Failed to fetch warmup bars for %s", symbol)

        if self.config.news.enabled and self._news_provider_codes:
            try:
                headlines = await fetch_recent_headlines(
                    self.ib, contract, self._news_provider_codes, self.config.news.lookback_hours
                )
                catalyst = classify_headlines(headlines)
                ctx.catalyst_category = catalyst.category
                ctx.catalyst_headline = catalyst.headline
            except Exception:
                self.logger.exception("Failed to fetch news for %s", symbol)

        self.contexts[symbol] = ctx
        self.contracts[symbol] = contract

        try:
            keep_updated = await self._request_live_updates(contract)
        except Exception:
            self.logger.exception(
                "Failed to subscribe to keep-updated bars for %s -- will retry on a later scan tick", symbol
            )
            # Undo the two lines above: without this, _scan_loop's `if symbol
            # not in self.contexts` gate treats this symbol as already
            # handled forever, even though it never got a live subscription
            # at all -- this was the original 2026-09-15 bug for the case
            # where the request fails outright rather than silently going
            # quiet after a nominal success (that case is instead caught
            # live by the staleness watchdog, since it never gets this far).
            self.contexts.pop(symbol, None)
            self.contracts.pop(symbol, None)
            return
        keep_updated.updateEvent += self._make_bar_update_handler(symbol, contract, ctx)
        self._subscriptions[symbol] = keep_updated
        self._last_bar_at[symbol] = datetime.now(timezone.utc)
        self.logger.info(
            "Onboarded %s: prior_close=%s avg_daily_volume=%s bars=%d catalyst=%s scanner_rank=%s",
            symbol,
            ctx.prior_close,
            ctx.avg_daily_volume,
            len(ctx.bars),
            ctx.catalyst_category or "none",
            ctx.scanner_rank,
        )

    async def _resubscribe_symbol(self, symbol: str) -> None:
        """Self-heals a symbol whose live feed has gone silent (see
        _check_stale_subscriptions) -- cancels whatever subscription object
        we're still holding (best-effort; it may already be dead
        server-side) and requests a fresh one, without touching the
        accumulated bar history/indicators in `ctx`. Covers both confirmed
        2026-09-15 failure modes: a subscription that silently stopped
        streaming after a live-line cap was hit, and the ~179-subscription
        burst killed by a transient IBKR "HMDS server disconnect" (error
        10182)."""
        contract = self.contracts.get(symbol)
        ctx = self.contexts.get(symbol)
        if contract is None or ctx is None:
            return
        self.logger.warning(
            "No live update received for %s in >%ds -- treating its subscription as dead, resubscribing",
            symbol,
            self.config.data_watchdog.stale_after_seconds,
        )
        old = self._subscriptions.pop(symbol, None)
        if old is not None:
            try:
                self.ib.cancelHistoricalData(old)
            except Exception:
                self.logger.exception("Failed to cancel stale subscription for %s before resubscribing", symbol)

        try:
            keep_updated = await self._request_live_updates(contract)
        except Exception:
            self.logger.exception("Resubscribe attempt failed for %s -- will retry next watchdog cycle", symbol)
            # Stamped even on failure so the next attempt waits a full
            # stale_after_seconds rather than firing again on the very next
            # (much shorter) check_interval_seconds tick -- a failing
            # resubscribe shouldn't turn into a tight retry loop hammering
            # IBKR (this is exactly the kind of pattern that trips the
            # historical-data pacing violations noted in ib_client.py).
            self._last_bar_at[symbol] = datetime.now(timezone.utc)
            return
        keep_updated.updateEvent += self._make_bar_update_handler(symbol, contract, ctx)
        self._subscriptions[symbol] = keep_updated
        self._last_bar_at[symbol] = datetime.now(timezone.utc)
        self.logger.info("Resubscribed %s live bar feed successfully", symbol)

    async def _check_stale_subscriptions(self) -> None:
        if not is_active_session():
            return
        cfg = self.config.data_watchdog
        now = datetime.now(timezone.utc)
        threshold = timedelta(seconds=cfg.stale_after_seconds)
        stale = [
            symbol
            for symbol, last_at in list(self._last_bar_at.items())
            if symbol in self._subscriptions and now - last_at > threshold
        ]
        for symbol in stale:
            await self._resubscribe_symbol(symbol)

    async def _data_watchdog_loop(self) -> None:
        cfg = self.config.data_watchdog
        if not cfg.enabled:
            return
        while True:
            if not self.ib.isConnected():
                await asyncio.sleep(5)
                continue
            try:
                await self._check_stale_subscriptions()
            except Exception:
                self.logger.exception("Data watchdog iteration failed")
            await asyncio.sleep(cfg.check_interval_seconds)

    def _on_new_bar(self, contract: Contract, ctx: SymbolContext) -> None:
        now = datetime.now(timezone.utc)
        self.position_manager.on_bar(ctx)
        for strategy in self.strategies:
            try:
                signal = strategy.evaluate(ctx, now)
            except Exception:
                self.logger.exception("Strategy %s failed evaluating %s", strategy.name, ctx.symbol)
                continue
            if signal is not None:
                self._handle_signal(contract, signal, strategy, now)

    def _handle_signal(
        self, contract: Contract, signal: Signal, strategy: BaseStrategy, now: datetime
    ) -> None:
        self._clamp_stop_to_conservative_max(signal)
        signal_id = self.journal.record_signal(signal)
        decision = self.risk_manager.evaluate(signal, now=now)
        self.journal.record_risk_decision(signal_id, decision)
        if not decision.accepted:
            self.journal.record_rejection(signal, decision.reason)
            self.logger.info("Rejected %s/%s: %s", signal.symbol, signal.strategy, decision.reason)
            # The setup was real; only capacity turned it away. Give the
            # symbol another look once the cooldown passes instead of
            # burning this strategy's one daily shot at it on a rejection.
            strategy.rearm_after_rejection(
                signal.symbol, now, self.config.risk.rejected_signal_cooldown_seconds
            )
            return
        self.logger.info(
            "%sAccepted %s/%s qty=%d entry=%.4f stop=%.4f target=%.4f",
            "[DRY RUN] " if self.dry_run else "",
            signal.symbol,
            signal.strategy,
            decision.sized_qty,
            signal.entry_price,
            signal.stop_price,
            signal.target_price,
        )
        if self.config.notifications.enabled and self.config.notifications.notify_on_signal:
            send_discord_message(
                f"📈 {'[DRY RUN] ' if self.dry_run else ''}"
                f"{signal.symbol} {signal.strategy} {signal.side} qty={decision.sized_qty} "
                f"entry=${signal.entry_price:.2f} stop=${signal.stop_price:.2f} target=${signal.target_price:.2f}",
                channel="trade_activity",
            )
        if self.dry_run:
            return
        self.order_manager.submit_signal(contract, signal, decision.sized_qty, signal_id)

    def _clamp_stop_to_conservative_max(self, signal: Signal) -> None:
        """Every accepted signal gets a conservative stop, regardless of
        which strategy produced it -- tightens (never loosens) whatever
        structural stop the strategy computed if it would risk more than
        max_stop_distance_pct of entry price. Long-only, per Signal's own
        docstring, so the conservative stop always sits below entry."""
        max_risk = signal.entry_price * self.config.risk.max_stop_distance_pct / 100.0
        if signal.risk_per_share > max_risk:
            # Post-construction mutation bypasses Signal.__post_init__'s own
            # rounding (that only runs once, at __init__) -- round here too,
            # or this unrounded value flows straight into the stop order's
            # stopPrice and IBKR rejects the whole bracket with error 110.
            signal.stop_price = round_to_tick(signal.entry_price - max_risk)

    def reset_daily_state(self) -> None:
        for strategy in self.strategies:
            strategy.reset_daily()
        # Drop every symbol's accumulated bar history along with its
        # subscription, so the next scan re-onboards it with fresh warmup
        # bars and a fresh prior close. Without this, `ctx.bars` just keeps
        # growing across the midnight boundary for any symbol still tracked
        # -- and every session-scoped number derived from it silently spans
        # two days: VWAP anchors to yesterday, cumulative volume (and so
        # relative volume) double-counts it, and prior_close stays the close
        # from the day before that.
        for symbol in list(self._subscriptions):
            self._unsubscribe_symbol(symbol, reason="daily_reset")
        self.contexts.clear()
        self.contracts.clear()
        self._last_bar_at.clear()
        self._last_scan_seen_at.clear()
        self.account_state.reset_session()
        snapshot = self.account_state.snapshot()
        self.risk_manager.mark_start_of_day(snapshot.net_liquidation)
        self.position_manager.clear()
        self._eod_flatten_fired = False
        self._loss_limit_flatten_fired = False
        self._last_logged_breadth = None
        self.logger.info("Daily state reset. Start-of-day equity=%.2f", snapshot.net_liquidation)


async def run() -> None:
    config = load_config()
    bot = WarriorBot(config)
    await bot.start()
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await bot.stop()


if __name__ == "__main__":
    asyncio.run(run())
