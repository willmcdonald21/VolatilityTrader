from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone

from ib_async import Contract, Order, Trade

from warrior_bot.broker.historical import fetch_prior_close, fetch_warmup_bars
from warrior_bot.broker.ib_client import IBClient
from warrior_bot.broker.market_data import ScannerRefused, scan_candidates
from warrior_bot.broker.news import discover_provider_codes, fetch_recent_headlines
from warrior_bot.config import AppConfig, load_config
from warrior_bot.execution.order_manager import OrderManager
from warrior_bot.execution.position_manager import PositionManager
from warrior_bot.logging_setup import alert, setup_logging
from warrior_bot.notify.discord import (
    flush as discord_flush,
    send_discord_message,
    validate_configured_channels,
)
from warrior_bot.persistence.db import get_connection
from warrior_bot.persistence.journal import Journal, _iso
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
from warrior_bot.utils.time_utils import (
    is_active_session,
    session_anchor,
    set_premarket_volume_share,
    to_eastern,
)

logger = logging.getLogger("warrior_bot.main")


def _bar_from_ib(b) -> Bar:
    return Bar(time=b.date, open=b.open, high=b.high, low=b.low, close=b.close, volume=b.volume)


def _split_exec_id(exec_id: str | None, row_id: int, row_count: int) -> str | None:
    """A distinct-but-deterministic exec_id for a split flatten fill.

    IBKR reports ONE execution, but a flatten covering a symbol with two
    lots is journaled as one row per lot -- and fills.exec_id is UNIQUE, so
    the second row would be silently dropped by INSERT OR IGNORE. Suffixing
    with the order row keeps each part unique while staying deterministic,
    so a re-delivered execution still dedups to the same rows rather than
    doubling the exit. A single, unsplit row keeps the raw id."""
    if exec_id is None or row_count <= 1:
        return exec_id
    return f"{exec_id}#{row_id}"


class WarriorBot:
    def __init__(self, config: AppConfig, dry_run: bool = False):
        self.config = config
        self.dry_run = dry_run
        self.logger = setup_logging(config)
        # Session model feeds relative volume everywhere; set before any
        # strategy can evaluate.
        set_premarket_volume_share(config.session.premarket_volume_share)
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
        # Symbols holding an open position when a reconnect dropped every
        # bar subscription. _scan_loop re-onboards these on its next tick
        # regardless of whether they still rank, so an open position never
        # loses its bar feed (and with it breakeven/trailing/reversal).
        self._resubscribe_after_reconnect: set[str] = set()
        # Scanner health. A refused scan is not an empty market (see
        # market_data.ScannerRefused) -- these drive alerting and the
        # forced reconnect that releases IBKR's leaked scanner slots.
        self._consecutive_scanner_refusals = 0
        self._last_successful_scan_at: datetime | None = None
        # Wall-clock time the connection state last changed, so a
        # disconnect can be reported with its duration instead of the loops
        # silently spinning (2026-09-28: 5h20m of near-total log silence).
        self._disconnected_since: datetime | None = None
        self._disconnect_alert_sent = False
        self._disconnect_logged_at: datetime | None = None
        # Productivity tracking for the heartbeat loop: last time any
        # bar arrived, and signals produced today.
        self._last_productive_at: datetime | None = None
        self._idle_alert_sent = False
        self._signals_today = 0
        self._last_equity_snapshot_at: datetime | None = None
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
            account_state=self.account_state,
            # Share this object's notion of the ET trading day, so a lot
            # closing during a rollover persists under the same date key
            # _restore_symbol_losses will read back.
            trading_date_provider=lambda: self._trading_day,
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
        self.float_provider = float_provider

        self.strategies: list[BaseStrategy] = []
        if config.strategies.gap_and_go.enabled:
            self.strategies.append(
                GapAndGoStrategy(
                    config.strategies.gap_and_go,
                    float_provider=float_provider,
                    pullback_quality_config=config.pullback_quality,
                )
            )
        if config.strategies.bull_flag.enabled:
            self.strategies.append(BullFlagStrategy(config.strategies.bull_flag, config.pullback_quality))
        if config.strategies.abcd.enabled:
            self.strategies.append(AbcdStrategy(config.strategies.abcd, config.pullback_quality))
        if config.strategies.vwap_reversion.enabled:
            self.strategies.append(VwapReversionStrategy(config.strategies.vwap_reversion, config.pullback_quality))
        if config.strategies.inverted_head_and_shoulders.enabled:
            self.strategies.append(
                InvertedHeadAndShouldersStrategy(config.strategies.inverted_head_and_shoulders)
            )

        self._scan_task: asyncio.Task | None = None
        self._risk_task: asyncio.Task | None = None
        self._watchdog_task: asyncio.Task | None = None
        self._reconciliation_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None
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
        self._trading_day = to_eastern(datetime.now(timezone.utc)).date()
        self._restore_or_start_daily_risk_state()
        self._restore_symbol_losses()
        self.journal.record_account_snapshot(snapshot)
        if self.config.news.enabled and not self._news_provider_codes:
            try:
                self._news_provider_codes = await discover_provider_codes(self.ib)
            except Exception:
                self.logger.exception("Failed to discover news providers")
        self._validate_notification_channels()
        self._validate_float_filter()
        self.ib_client.start_heartbeat()
        self._scan_task = asyncio.ensure_future(self._scan_loop())
        self._risk_task = asyncio.ensure_future(self._risk_loop())
        self._watchdog_task = asyncio.ensure_future(self._data_watchdog_loop())
        self._reconciliation_task = asyncio.ensure_future(self._position_reconciliation_loop())
        self._heartbeat_task = asyncio.ensure_future(self._heartbeat_loop())
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
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
        # Drain queued notifications before the daemon worker dies with
        # the interpreter -- a shutdown alert is exactly the one you
        # cannot afford to lose.
        discord_flush(timeout=5.0)
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
        # Only the subscription teardown is conditional on having tracked
        # symbols. The two resyncs below must run unconditionally: they
        # recover fill listeners for OPEN POSITIONS, which can exist while
        # `contexts` is empty (right after reset_daily_state, after a mass
        # eviction, or before the first scan tick completes). Returning
        # early here skipped both and left those positions permanently
        # unmanaged.
        if self.contexts:
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

        tracked = self.position_manager.tracked_symbols()
        claimed = self.position_manager.resync_after_reconnect(self.ib)
        self.order_manager.resync_open_orders(force=True, skip_order_ids=frozenset(claimed))

        if tracked and not claimed:
            alert(
                f"Reconnected but resynced 0 orders while still tracking {len(tracked)} position(s) "
                f"({', '.join(sorted(tracked))}) -- fill tracking may be dead; check IBKR",
                channel="kill_switch",
            )

        # Held symbols must be re-subscribed regardless of the scanner.
        # _scan_loop only onboards names in the current top-N, so a position
        # that had dropped out of the scan (very likely after a multi-hour
        # outage) never got bars again -- meaning no breakeven, no trailing
        # and no reversal exit for it for the rest of the day, leaving only
        # its resting stop. On 2026-09-28 the scanner itself was dead after
        # the reconnect, so nothing would have been re-onboarded at all.
        if tracked:
            self._resubscribe_after_reconnect = set(tracked)

    # Consecutive refused scans before forcing a reconnect. The leaked
    # scanner slots that caused the 2026-09-28 blackout are held per API
    # connection, so dropping and re-establishing the connection is what
    # actually releases them -- confirmed by the manual restart that
    # recovered it. Kept high enough that a transient refusal doesn't churn
    # the connection: at refresh_seconds=5 this is ~1 minute of failure.
    SCANNER_REFUSAL_RECONNECT_THRESHOLD = 12

    # A disconnect lasting longer than this during an active session is
    # escalated from a log line to an alert. Below it, brief blips stay
    # quiet.
    DISCONNECT_ALERT_SECONDS = 120.0

    # No bars at all for this long during an active session means the bot
    # is up but blind -- a dead scanner, a wedged feed, or an empty
    # watchlist. Generous enough that a genuinely thin pre-market tape on a
    # handful of symbols doesn't cry wolf.
    IDLE_ALERT_SECONDS = 600.0
    HEARTBEAT_INTERVAL_SECONDS = 60.0

    async def _heartbeat_loop(self) -> None:
        """Records liveness once a minute and alerts when the bot is up but
        doing nothing.

        Every existing watchdog covers CONNECTION health. None covered
        PRODUCTIVITY -- a bot that is connected, subscribed and silently
        producing nothing is indistinguishable from a quiet market. On
        2026-09-28 it sat disconnected for 5h20m, came back onto a dead
        scanner for another six hours, and the only thing that surfaced it
        was someone asking."""
        while True:
            await asyncio.sleep(self.HEARTBEAT_INTERVAL_SECONDS)
            try:
                self._record_heartbeat()
            except Exception:
                self.logger.exception("Heartbeat iteration failed")

    def _record_heartbeat(self) -> None:
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(seconds=self.HEARTBEAT_INTERVAL_SECONDS)
        bars_recently = sum(1 for seen_at in self._last_bar_at.values() if seen_at >= cutoff)
        connected = self.ib.isConnected()
        seconds_since_scan = (
            (now - self._last_successful_scan_at).total_seconds() if self._last_successful_scan_at else None
        )

        self.journal.record_heartbeat(
            connected=connected,
            symbols_subscribed=len(self._subscriptions),
            bars_received_last_min=bars_recently,
            signals_today=self._signals_today,
            open_positions=len(self.position_manager.tracked_symbols()),
            breadth=self._last_logged_breadth,
            scanner_refusals=self._consecutive_scanner_refusals,
            seconds_since_scan=seconds_since_scan,
        )

        if not connected or not is_active_session():
            self._last_productive_at = self._last_productive_at or now
            return

        if bars_recently > 0:
            self._last_productive_at = now
            self._idle_alert_sent = False
            return

        if self._last_productive_at is None:
            self._last_productive_at = now
            return

        idle_for = (now - self._last_productive_at).total_seconds()
        if idle_for >= self.IDLE_ALERT_SECONDS and not self._idle_alert_sent:
            self._idle_alert_sent = True
            self.logger.error(
                "No bars received from any of %d subscribed symbol(s) in %.0f minutes during an active session",
                len(self._subscriptions),
                idle_for / 60,
            )
            alert(
                f"Bot is CONNECTED but has received no market data for {idle_for / 60:.0f} minutes "
                f"({len(self._subscriptions)} symbols subscribed, "
                f"{self._consecutive_scanner_refusals} scanner refusals) -- it is up but blind",
                channel="kill_switch",
            )

    def _validate_float_filter(self) -> None:
        """Says so out loud when the float filter is enabled but has no
        data to work with.

        config.yaml sets enable_float_filter: true and annotates it
        "matches Ross Cameron's 5 Pillars filter", but FloatProvider
        degrades to "allow everything" when config/float_list.csv is
        missing -- which it is. The result: a filter the operator believes
        is screening out large-float names has never rejected a single
        symbol (0 of 2,311 signals carry any float data), and the only
        notice was one logger.info buried in millions of lines."""
        if not self.config.strategies.gap_and_go.enable_float_filter:
            return
        if self.float_provider.is_available():
            return
        alert(
            "Float filter is ENABLED but no usable float data exists "
            f"({self.config.resolve_path('config/float_list.csv')}) -- "
            "every symbol is passing the low-float check unfiltered",
            channel="kill_switch",
        )

    def _validate_notification_channels(self) -> None:
        """Warns at startup about enabled Discord channels with no webhook
        URL configured. A missing env var used to be a silent no-op --
        indistinguishable from working until the moment an alert didn't
        arrive."""
        cfg = self.config.notifications
        if not cfg.enabled:
            return
        enabled = []
        if cfg.notify_on_kill_switch:
            enabled.append("kill_switch")
        if cfg.notify_on_limits:
            enabled.append("limits")
        if cfg.notify_on_signal or cfg.notify_on_fill:
            enabled.append("trade_activity")
        if cfg.notify_on_entry_summary:
            enabled.append("trade_activity_summary")
        if cfg.notify_on_pnl:
            enabled.append("pnl")

        missing = validate_configured_channels(enabled)
        if missing:
            self.logger.error(
                "Notifications are enabled for %s but no webhook URL is configured for them -- "
                "those alerts will go nowhere",
                ", ".join(missing),
            )
            if "kill_switch" not in missing:
                alert(
                    f"Discord channels enabled with no webhook configured: {', '.join(missing)} -- "
                    "those notifications are being silently discarded",
                    channel="kill_switch",
                )

    async def _await_connection(self, loop_name: str) -> bool:
        """Returns True when connected. When not, records and reports the
        outage instead of silently sleeping.

        Every loop used to do a bare `if not isConnected(): sleep(5);
        continue`, so an outage produced no scanning, no evaluations and
        essentially no log evidence -- 2026-09-28 lost 5h20m that way, with
        3-15 log lines an hour and no alert. All four loops share this
        state, so the reporting is throttled once globally rather than per
        loop."""
        now = datetime.now(timezone.utc)

        if self.ib.isConnected():
            if self._disconnected_since is not None:
                outage = (now - self._disconnected_since).total_seconds()
                self._disconnected_since = None
                self._disconnect_alert_sent = False
                self._disconnect_logged_at = None
                self.logger.warning("IBKR connection restored after %.0fs offline", outage)
                if outage >= self.DISCONNECT_ALERT_SECONDS:
                    alert(f"IBKR connection restored after {outage / 60:.1f} minutes offline", channel="kill_switch")
            return True

        if self._disconnected_since is None:
            self._disconnected_since = now
            self._disconnect_logged_at = now
            self.logger.warning("%s: IBKR not connected -- loops idle until it returns", loop_name)
            return False

        outage = (now - self._disconnected_since).total_seconds()
        since_logged = (now - self._disconnect_logged_at).total_seconds() if self._disconnect_logged_at else None
        if since_logged is None or since_logged >= 60:
            self._disconnect_logged_at = now
            self.logger.warning("Still disconnected from IBKR after %.0fs (noticed by %s)", outage, loop_name)
        if outage >= self.DISCONNECT_ALERT_SECONDS and not self._disconnect_alert_sent and is_active_session():
            self._disconnect_alert_sent = True
            alert(
                f"IBKR has been disconnected for {outage / 60:.1f} minutes during an active session -- "
                "the bot is not scanning, evaluating or managing positions",
                channel="kill_switch",
            )
        return False

    async def _scan_loop(self) -> None:
        while True:
            if not await self._await_connection("scan_loop"):
                await asyncio.sleep(5)
                continue
            try:
                try:
                    # NOT wrapped in asyncio.wait_for: scan_candidates owns
                    # its timeout so it can cancel the scanner subscription
                    # on the way out. Timing it out from here is what leaked
                    # all ten of IBKR's slots on 2026-09-28 and again on
                    # 2026-10-01 -- see scan_candidates' docstring.
                    symbols = await scan_candidates(self.ib, self.config, timeout=30)
                except ScannerRefused as exc:
                    await self._handle_scanner_refusal(exc)
                    await asyncio.sleep(self.config.scanner.refresh_seconds)
                    continue
                self._note_scan_succeeded()
                now = datetime.now(timezone.utc)
                # Symbols holding an open position across a reconnect are
                # onboarded first and unconditionally -- see _on_connected.
                # They are prepended rather than merged so their rank still
                # reflects the real scan when they are genuinely in it.
                for held in sorted(self._resubscribe_after_reconnect - set(symbols)):
                    if held not in self.contexts:
                        self.logger.warning(
                            "Re-onboarding %s after reconnect: it holds an open position but is no "
                            "longer in the scanner's results",
                            held,
                        )
                        await self._onboard_symbol(held, scanner_rank=None)
                self._resubscribe_after_reconnect.clear()
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

    def _note_scan_succeeded(self) -> None:
        if self._consecutive_scanner_refusals:
            self.logger.info(
                "Scanner recovered after %d refused scan(s)", self._consecutive_scanner_refusals
            )
            alert(
                f"Scanner recovered after {self._consecutive_scanner_refusals} refused scan(s)",
                channel="kill_switch",
            )
        self._consecutive_scanner_refusals = 0
        self._last_successful_scan_at = datetime.now(timezone.utc)

    async def _handle_scanner_refusal(self, exc: Exception) -> None:
        """IBKR refused the scan. Crucially this does NOT demote any
        symbol's rank: an empty result used to clear every rank, and since
        the 2026-09-26 eligibility gate a cleared rank makes a symbol
        ineligible for every strategy -- so a scanner failure silently took
        the whole strategy layer offline rather than just pausing
        discovery. Existing ranks are left exactly as they were until a
        real scan supersedes them."""
        self._consecutive_scanner_refusals += 1
        count = self._consecutive_scanner_refusals
        self.logger.error("Scanner refused (%d consecutive): %s", count, exc)

        # Alert once when it starts, then at a slow cadence -- at
        # refresh_seconds=5 this is roughly once a minute.
        if count == 1 or count % self.SCANNER_REFUSAL_RECONNECT_THRESHOLD == 0:
            alert(
                f"Scanner is being REFUSED by IBKR ({count} consecutive): {exc}. "
                "No new candidates are being discovered and no strategy can fire.",
                channel="kill_switch",
            )

        if count and count % self.SCANNER_REFUSAL_RECONNECT_THRESHOLD == 0:
            # Scanner subscription slots leak per API connection, so a
            # reconnect is what actually frees them.
            self.logger.warning("Forcing an IBKR reconnect to release leaked scanner subscriptions")
            alert("Forcing an IBKR reconnect to clear the scanner refusal", channel="kill_switch")
            try:
                self.ib.disconnect()  # disconnectedEvent drives the normal reconnect path
            except Exception:
                self.logger.exception("Failed to force a disconnect for scanner recovery")

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
            if not await self._await_connection("risk_loop"):
                await asyncio.sleep(5)
                continue
            try:
                self._check_new_trading_day()
                self._check_flatten_triggers()
                self._maybe_record_equity()
                self.position_manager.cancel_stale_entries(
                    self.config.risk.entry_fill_timeout_seconds
                )
            except Exception:
                self.logger.exception("Risk loop iteration failed")
            await asyncio.sleep(self.config.exits.risk_loop_interval_seconds)

    # account_snapshots was written exactly once, in start() -- 46 rows in
    # six weeks, one per process restart. There was therefore no equity
    # curve at all: no max drawdown, no time-to-recovery, no intraday
    # excursion, and no independent check on the fill-reconstructed P&L.
    # Once a minute (rather than every 15s risk tick) is ample resolution
    # and matches the heartbeat cadence.
    EQUITY_SNAPSHOT_INTERVAL_SECONDS = 60.0

    def _maybe_record_equity(self) -> None:
        now = datetime.now(timezone.utc)
        if (
            self._last_equity_snapshot_at is not None
            and (now - self._last_equity_snapshot_at).total_seconds() < self.EQUITY_SNAPSHOT_INTERVAL_SECONDS
        ):
            return
        snapshot = self.account_state.snapshot()
        if snapshot.net_liquidation is None:
            return  # nothing worth recording; see AccountState._account_value
        self._last_equity_snapshot_at = now
        self.journal.record_account_snapshot(snapshot)

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
        # Set BEFORE reset_daily_state(), not after -- that method persists
        # the new day's risk baseline keyed by self._trading_day, and must
        # see today's date, not the one that just ended.
        self._trading_day = now_et_date
        self.reset_daily_state()

    def _restore_or_start_daily_risk_state(self) -> None:
        """Restores today's persisted risk baseline/halt if this process
        has already established one (a same-day restart), otherwise
        establishes a fresh one -- the first genuine start of this trading
        day. The distinction matters: mark_start_of_day always clears the
        halt, which is correct once per real trading day and wrong on
        every other restart within it (see RiskManager.load_state's
        docstring for the live incident this fixes)."""
        persisted = self.journal.load_daily_risk_state(self._trading_day.isoformat())
        if persisted is not None:
            self.risk_manager.load_state(**persisted)
            self.logger.info(
                "Restored daily risk state for %s: start_of_day_equity=%.2f halted=%s",
                self._trading_day,
                persisted["start_of_day_equity"],
                persisted["loss_limit_halted"],
            )
        else:
            snapshot = self.account_state.snapshot()
            self.risk_manager.mark_start_of_day(snapshot.net_liquidation)
            self._persist_daily_risk_state()

    def _restore_symbol_losses(self) -> None:
        """Rehydrates the symbol_loss_cap gate's per-symbol losing-lot
        counts for today, so a crash-restart does not re-open every
        symbol that already took money off us this session.

        Kept separate from _restore_or_start_daily_risk_state above
        because that method's restore/establish branch is specifically
        about the first genuine start of a trading day versus a restart
        within it. The loss counts have no such distinction -- an absent
        row just means zero -- and nesting them under that `if` would
        skip restoration on a day's first start.

        No periodic re-persist is needed (unlike the halt flag): counts
        are written synchronously the moment a lot closes red."""
        counts = self.journal.load_symbol_losses(self._trading_day.isoformat())
        self.position_manager.restore_daily_losses(counts)
        if counts:
            self.logger.info("Restored symbol loss state for %s: %s", self._trading_day, counts)

    def _persist_daily_risk_state(self) -> None:
        equity = self.risk_manager.start_of_day_equity
        if equity is None:
            return
        self.journal.save_daily_risk_state(
            self._trading_day.isoformat(), equity, self.risk_manager.loss_limit_halted_today
        )

    def _check_flatten_triggers(self) -> None:
        # Cheap and idempotent -- run every tick so a halt that trips this
        # cycle is on disk before the next same-day restart, whatever
        # triggers it.
        self._persist_daily_risk_state()
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

        if lots and total_qty > 0:
            # Attributable: split proportionally across this symbol's lots.
            row_shares = [
                (
                    self.journal.record_order(
                        signal_id=lot.signal_id,
                        symbol=symbol,
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
        else:
            # Unattributable, and recorded anyway. This branch used to
            # `return`, because orders.signal_id was NOT NULL and there was
            # no signal to point at -- which is the single biggest reason
            # only ~45% of traded notional had a journaled exit, and why
            # there were 2 emergency_flatten rows in six weeks. The shares
            # genuinely left the account; the row belongs in the journal
            # with signal_id NULL and the symbol carrying the attribution.
            self.logger.warning(
                "Flatten fill for %s has no tracked lot -- journaling it against the symbol "
                "with no signal attribution",
                symbol,
            )
            row_shares = [
                (
                    self.journal.record_order(
                        signal_id=None,
                        symbol=symbol,
                        ib_order_id=order.orderId,
                        role="emergency_flatten",
                        action=order.action,
                        qty=order.totalQuantity,
                        order_type=order.orderType,
                        limit_price=getattr(order, "lmtPrice", None),
                        stop_price=None,
                        oca_group=None,
                        status=trade.orderStatus.status,
                    ),
                    1.0,
                )
            ]

        def on_fill(t, fill) -> None:
            for row_id, share in row_shares:
                self.journal.record_fill(
                    order_row_id=row_id,
                    ib_order_id=order.orderId,
                    fill_qty=fill.execution.shares * share,
                    fill_price=fill.execution.price,
                    exec_id=_split_exec_id(getattr(fill.execution, "execId", None), row_id, len(row_shares)),
                    exec_ts=_iso(getattr(fill.execution, "time", None)),
                )

        trade.fillEvent += on_fill

    async def _position_reconciliation_loop(self) -> None:
        cfg = self.config.position_reconciliation
        if not cfg.enabled:
            return
        while True:
            if not await self._await_connection("data_watchdog"):
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
        # This account, equities only. Three filters, because each covers a
        # case the others do not.
        #
        # (1) account: ib.positions() is account-wide, not per-client, so once a
        # second account is linked under this username it returns both. Blank
        # means "the only account", which is today's behaviour.
        #
        # (2) secType: another bot trades options with bot-managed synthetic
        # stops -- it rests no broker-side stop at all, so every one of its
        # positions reads as uncovered here and would be flattened within one
        # check interval. This bot is long-only equities, so nothing it actually
        # trades is excluded.
        #
        # (3) non-zero: a closed position still reports a row.
        #
        # The dict is also keyed by symbol, so without the secType filter an SPY
        # stock position and an SPY option position would collapse into one entry
        # and silently drop whichever lost -- disabling the NRXS backstop for it.
        live_positions = {
            p.contract.symbol: p
            for p in self.ib.positions(account=self.config.trading.account)
            if p.position != 0 and p.contract.secType == "STK"
        }

        # Stale local tracking: PositionManager thinks a symbol is still
        # open but IBKR shows it flat (e.g. a resync that couldn't resolve
        # a stop that filled/cancelled entirely while disconnected).
        #
        # A lot whose ENTRY is still working is not stale -- IBKR correctly
        # reports no position for it yet. track() registers a lot the
        # instant the bracket is submitted with remaining_qty=0, and
        # entry_fill_timeout_seconds is 300s (RLGT filled 3h56m late on
        # 2026-09-15), so before this guard any entry slower than one 30s
        # watchdog cycle was silently untracked. It then kept its stop but
        # lost breakeven, trailing and reversal-exit permanently (on_bar
        # can no longer see it), stopped counting toward
        # max_concurrent_positions / the 2-lot cap / the cross-strategy
        # gate, and became invisible to cancel_stale_entries -- the very
        # mechanism meant to cancel it.
        for symbol in self.position_manager.tracked_symbols() - set(live_positions.keys()):
            if self.position_manager.has_unfilled_entry(symbol):
                continue
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
            if self._stop_is_stranded(trade):
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

    # How far below its own trigger price a stop-limit has to be left before
    # it counts as stranded rather than protective. Every stop this bot
    # places is a STP LMT with the limit only stop_limit_offset_pct (0.5%)
    # below the trigger; in a gap-down on a low-float name the stop triggers
    # and the limit is left behind unfilled. The order keeps reporting its
    # full `remaining`, so the watchdog scored the position 100% covered
    # while it was in fact naked. Sized comfortably beyond the limit offset
    # so ordinary trading around the trigger doesn't read as stranded.
    _STRANDED_STOP_PCT = 2.0

    def _stop_is_stranded(self, trade) -> bool:
        """True when a stop has triggered but its limit was left behind, so
        it is no longer providing the protection its `remaining` implies."""
        trigger = getattr(trade.order, "auxPrice", None)
        if not trigger or trigger <= 0 or trigger > 1e15:  # 1e15: IBKR's UNSET_DOUBLE sentinel
            return False
        if (trade.orderStatus.filled or 0) > 0:
            return False  # partially filling -- it is working, not stranded
        ctx = self.contexts.get(trade.contract.symbol)
        last_price = ctx.last_price if ctx is not None else None
        if last_price is None or last_price <= 0:
            return False  # no trustworthy reference; leave the old behaviour
        stranded = last_price < trigger * (1 - self._STRANDED_STOP_PCT / 100.0)
        if stranded:
            self.logger.warning(
                "Reconciliation: %s stop triggered at %.4f but price is %.4f with no fills -- "
                "treating it as stranded, not protection",
                trade.contract.symbol,
                trigger,
                last_price,
            )
        return stranded

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

        # Anchored at onboarding rather than left None so VWAP and session
        # volume measure the same session for every symbol regardless of when
        # the scanner found it -- and so a reconnect-driven re-onboard at
        # 11:00 doesn't silently re-anchor a symbol's VWAP to 11:00.
        ctx = SymbolContext(symbol=symbol, scanner_rank=scanner_rank, session_anchor=session_anchor())

        try:
            ctx.prior_close = await asyncio.wait_for(fetch_prior_close(self.ib, contract), timeout=30)
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
            warmup_bars = await asyncio.wait_for(
                fetch_warmup_bars(self.ib, contract, self.config), timeout=30
            )
            for b in warmup_bars:
                ctx.add_bar(_bar_from_ib(b))
        except Exception:
            self.logger.exception("Failed to fetch warmup bars for %s", symbol)

        if self.config.news.enabled and self._news_provider_codes:
            try:
                headlines = await asyncio.wait_for(
                    fetch_recent_headlines(
                        self.ib, contract, self._news_provider_codes, self.config.news.lookback_hours
                    ),
                    timeout=30,
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
            if not await self._await_connection("position_reconciliation"):
                await asyncio.sleep(5)
                continue
            try:
                await self._check_stale_subscriptions()
            except Exception:
                self.logger.exception("Data watchdog iteration failed")
            await asyncio.sleep(cfg.check_interval_seconds)

    def _eligible_for_new_signals(self, ctx: SymbolContext) -> bool:
        """Ross Cameron explicitly trades only the top 2-3 (occasionally top
        5) most obvious gainers each morning -- until now, every onboarded
        scanner candidate was equally eligible for every strategy regardless
        of rank (see ScannerConfig.max_eligible_rank). A symbol whose rank
        moves outside the cutoff (or that isn't currently ranked at all --
        e.g. it dropped out of the scanner's top-N) simply stops producing
        NEW signals; on_bar above still manages any already-open position on
        it normally, since this gate only runs on the strategy-evaluation
        path below."""
        max_rank = self.config.scanner.max_eligible_rank
        if max_rank is None:
            return True
        return ctx.scanner_rank is not None and ctx.scanner_rank <= max_rank

    def _on_new_bar(self, contract: Contract, ctx: SymbolContext) -> None:
        now = datetime.now(timezone.utc)
        self.position_manager.on_bar(ctx)
        if not self._eligible_for_new_signals(ctx):
            return
        for strategy in self.strategies:
            try:
                signal = strategy.evaluate(ctx, now)
            except Exception:
                self.logger.exception("Strategy %s failed evaluating %s", strategy.name, ctx.symbol)
                continue
            if signal is not None:
                self._signals_today += 1
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
        self._persist_daily_risk_state()
        self.position_manager.clear()
        # Separate from clear() on purpose: clear() also runs mid-day on a
        # flatten, where forgetting which symbols already burnt us would
        # re-open every one of them.
        self.position_manager.reset_daily_losses()
        self._eod_flatten_fired = False
        self._loss_limit_flatten_fired = False
        self._last_logged_breadth = None
        self._signals_today = 0
        self._idle_alert_sent = False
        self._last_productive_at = None
        self.logger.info("Daily state reset. Start-of-day equity=%s", snapshot.net_liquidation)


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
