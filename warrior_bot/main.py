from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone

from ib_async import Contract

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
from warrior_bot.utils.panic import panic_stop
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
            self.ib, self.journal, config.exits, stop_limit_offset_pct=config.execution.stop_limit_offset_pct
        )
        self.risk_manager = RiskManager(
            config.risk, self.account_state, self.position_manager, config.resolve_path(config.kill_switch.flag_file)
        )
        self.order_manager = OrderManager(
            self.ib,
            self.journal,
            config.exits,
            self.position_manager,
            execution_config=config.execution,
            notifications_config=config.notifications,
            account_state=self.account_state,
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
        self._flattened_today = False
        self._news_provider_codes = config.news.provider_codes
        self._last_logged_breadth: int | None = None
        # ET calendar date this process last reset daily state for. This is
        # what makes EOD flatten (and everything else in reset_daily_state)
        # keep working every day for a long-running process -- without it,
        # _flattened_today only ever gets cleared once, in __init__, so a
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
        self.ib_client.disconnect()

    def _on_connected(self) -> None:
        """Fires on every successful (re)connect, including reconnects --
        ib_async's own disconnect() docs state it "will clear all session
        state", which silently kills every symbol's live bar subscription
        without raising anything. A no-op on the very first connect (there's
        nothing tracked yet); on a reconnect, drops already-tracked symbols
        so `_scan_loop` treats them as new again and re-onboards them --
        fresh contract qualification, warmup bars, and a live bar
        subscription on the new session. Never touches `position_manager`:
        already-open bracket orders are standing orders IBKR keeps working
        server-side independent of our API session."""
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

    async def _risk_loop(self) -> None:
        while True:
            if not self.ib.isConnected():
                await asyncio.sleep(5)
                continue
            try:
                self._check_new_trading_day()
                self._check_flatten_triggers()
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
        if self._flattened_today:
            return
        now_et = to_eastern(datetime.now(timezone.utc))
        if now_et.time() >= self.config.exits.eod_flatten_time:
            self._trigger_flatten("eod_flatten")
            return
        snapshot = self.account_state.snapshot()
        if self.risk_manager.should_flatten_for_loss_limit(snapshot):
            self._trigger_flatten("daily_loss_limit")

    def _trigger_flatten(self, reason: str) -> None:
        self.logger.warning("Flattening all positions: reason=%s", reason)
        alert(f"Flattening all positions and stopping for the day: reason={reason}", channel="limits")
        panic_stop(self.ib, flatten=True, channel="limits")
        self.position_manager.clear()
        self.journal.record_kill_switch_event(triggered_by=reason, action_taken="cancel_all+flatten_all")
        self._flattened_today = True

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
        the one place in the callback chain without a safety net."""

        def on_update(bars, has_new_bar) -> None:
            self._last_bar_at[symbol] = datetime.now(timezone.utc)
            if not has_new_bar or not bars:
                return
            try:
                ctx.add_bar(_bar_from_ib(bars[-1]))
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
                self._handle_signal(contract, signal)

    def _handle_signal(self, contract: Contract, signal: Signal) -> None:
        self._clamp_stop_to_conservative_max(signal)
        signal_id = self.journal.record_signal(signal)
        decision = self.risk_manager.evaluate(signal)
        self.journal.record_risk_decision(signal_id, decision)
        if not decision.accepted:
            self.journal.record_rejection(signal, decision.reason)
            self.logger.info("Rejected %s/%s: %s", signal.symbol, signal.strategy, decision.reason)
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
        self.account_state.reset_session()
        snapshot = self.account_state.snapshot()
        self.risk_manager.mark_start_of_day(snapshot.net_liquidation)
        self.position_manager.clear()
        self._flattened_today = False
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
