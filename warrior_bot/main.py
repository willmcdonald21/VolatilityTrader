from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

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
from warrior_bot.signals.signal import Signal
from warrior_bot.strategies.abcd_pattern import AbcdStrategy
from warrior_bot.strategies.base_strategy import BaseStrategy, SymbolContext
from warrior_bot.strategies.bull_flag import BullFlagStrategy
from warrior_bot.strategies.gap_and_go import GapAndGoStrategy
from warrior_bot.strategies.indicators import Bar
from warrior_bot.strategies.vwap_reversion import VwapReversionStrategy
from warrior_bot.utils.panic import panic_stop
from warrior_bot.utils.time_utils import to_eastern

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

        conn = get_connection(config.resolve_path(config.journal.db_path))
        self.journal = Journal(conn)
        self.account_state = AccountState(self.ib)
        self.risk_manager = RiskManager(
            config.risk, self.account_state, config.resolve_path(config.kill_switch.flag_file)
        )
        self.position_manager = PositionManager(
            self.ib, self.journal, config.exits, stop_limit_offset_pct=config.execution.stop_limit_offset_pct
        )
        self.order_manager = OrderManager(
            self.ib,
            self.journal,
            config.exits,
            self.position_manager,
            execution_config=config.execution,
            notifications_config=config.notifications,
        )

        float_provider = FloatProvider(config.resolve_path("config/float_list.csv"))

        self.strategies: list[BaseStrategy] = []
        if config.strategies.gap_and_go.enabled:
            self.strategies.append(GapAndGoStrategy(config.strategies.gap_and_go, float_provider=float_provider))
        if config.strategies.bull_flag.enabled:
            self.strategies.append(BullFlagStrategy(config.strategies.bull_flag))
        if config.strategies.abcd.enabled:
            self.strategies.append(AbcdStrategy(config.strategies.abcd))
        if config.strategies.vwap_reversion.enabled:
            self.strategies.append(VwapReversionStrategy(config.strategies.vwap_reversion))

        self._scan_task: asyncio.Task | None = None
        self._risk_task: asyncio.Task | None = None
        self._flattened_today = False
        self._news_provider_codes = config.news.provider_codes

    async def start(self) -> None:
        await self.ib_client.connect()
        snapshot = self.account_state.snapshot()
        self.risk_manager.mark_start_of_day(snapshot.net_liquidation)
        self.journal.record_account_snapshot(snapshot)
        if self.config.news.enabled and not self._news_provider_codes:
            try:
                self._news_provider_codes = await discover_provider_codes(self.ib)
            except Exception:
                self.logger.exception("Failed to discover news providers")
        self._scan_task = asyncio.ensure_future(self._scan_loop())
        self._risk_task = asyncio.ensure_future(self._risk_loop())
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
        self.ib_client.disconnect()

    async def _scan_loop(self) -> None:
        while True:
            if not self.ib.isConnected():
                await asyncio.sleep(5)
                continue
            try:
                symbols = await scan_candidates(self.ib, self.config)
                for symbol in symbols:
                    if symbol not in self.contexts:
                        await self._onboard_symbol(symbol)
            except Exception:
                self.logger.exception("Scan loop iteration failed")
            await asyncio.sleep(self.config.scanner.refresh_seconds)

    async def _risk_loop(self) -> None:
        while True:
            if not self.ib.isConnected():
                await asyncio.sleep(5)
                continue
            try:
                self._check_flatten_triggers()
            except Exception:
                self.logger.exception("Risk loop iteration failed")
            await asyncio.sleep(self.config.exits.risk_loop_interval_seconds)

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

    async def _onboard_symbol(self, symbol: str) -> None:
        try:
            contract = await self.ib_client.qualify_stock(symbol)
        except Exception:
            self.logger.exception("Could not qualify contract for %s", symbol)
            return

        ctx = SymbolContext(symbol=symbol)

        try:
            ctx.prior_close = await fetch_prior_close(self.ib, contract)
        except Exception:
            self.logger.exception("Failed to fetch prior close for %s", symbol)

        try:
            daily_bars = await self.ib.reqHistoricalDataAsync(
                contract,
                endDateTime="",
                durationStr="20 D",
                barSizeSetting="1 day",
                whatToShow="TRADES",
                useRTH=True,
                formatDate=2,
                keepUpToDate=False,
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

        def on_update(bars, has_new_bar, _symbol=symbol, _contract=contract, _ctx=ctx) -> None:
            if not has_new_bar or not bars:
                return
            _ctx.add_bar(_bar_from_ib(bars[-1]))
            self._on_new_bar(_contract, _ctx)

        keep_updated = await self.ib.reqHistoricalDataAsync(
            contract,
            endDateTime="",
            durationStr="3600 S",
            barSizeSetting="1 min",
            whatToShow="TRADES",
            useRTH=self.config.trading.use_rth,
            formatDate=2,
            keepUpToDate=True,
        )
        keep_updated.updateEvent += on_update
        self._subscriptions[symbol] = keep_updated
        self.logger.info(
            "Onboarded %s: prior_close=%s avg_daily_volume=%s bars=%d catalyst=%s",
            symbol,
            ctx.prior_close,
            ctx.avg_daily_volume,
            len(ctx.bars),
            ctx.catalyst_category or "none",
        )

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
                self._handle_signal(contract, signal, now)

    def _handle_signal(self, contract: Contract, signal: Signal, now: datetime) -> None:
        signal_id = self.journal.record_signal(signal)
        decision = self.risk_manager.evaluate(signal, now=now)
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

    def reset_daily_state(self) -> None:
        for strategy in self.strategies:
            strategy.reset_daily()
        self.account_state.reset_session()
        snapshot = self.account_state.snapshot()
        self.risk_manager.mark_start_of_day(snapshot.net_liquidation)
        self.position_manager.clear()
        self._flattened_today = False
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
