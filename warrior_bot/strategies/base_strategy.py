from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from warrior_bot.strategies.indicators import (
    Bar,
    average_true_range,
    ema,
    gap_pct,
    macd,
    opening_range,
    relative_volume,
    vwap,
)
from warrior_bot.signals.signal import Signal


@dataclass
class SymbolContext:
    """Shared per-symbol market state, computed once per bar and read by
    every strategy — avoids each strategy recomputing VWAP/rel-volume/etc.
    independently."""

    symbol: str
    bars: list[Bar] = field(default_factory=list)
    prior_close: float | None = None
    avg_daily_volume: float | None = None
    catalyst_category: str | None = None
    catalyst_headline: str | None = None
    # 1-based scanner rank ("obviousness") at the moment this symbol was
    # onboarded -- e.g. 1 means it was the single leading % gainer that
    # scan tick. Captured once, like catalyst_category, not re-checked
    # per bar.
    scanner_rank: int | None = None

    def add_bar(self, bar: Bar) -> None:
        self.bars.append(bar)

    @property
    def last_price(self) -> float | None:
        return self.bars[-1].close if self.bars else None

    @property
    def cumulative_volume(self) -> float:
        return sum(b.volume for b in self.bars)

    @property
    def vwap(self) -> float | None:
        return vwap(self.bars)

    @property
    def ema_9(self) -> float | None:
        return ema(self.bars, 9)

    @property
    def ema_20(self) -> float | None:
        return ema(self.bars, 20)

    @property
    def ema_200(self) -> float | None:
        """Completes the source material's confirmed indicator set
        (9/20/200 EMA + VWAP + MACD + volume + candlesticks). Not wired
        into any gate -- no concrete filter rule was ever given for it
        (unlike the 9 EMA pullback-hold gate), and with only 60 minutes of
        warmup bars (`fetch_warmup_bars`), it's typically still None for
        most of this bot's actual 7-10am ET trading window anyway. Exposed
        for context/journaling, not decision-making."""
        return ema(self.bars, 200)

    @property
    def gap_pct(self) -> float | None:
        if self.prior_close is None or self.last_price is None:
            return None
        return gap_pct(self.prior_close, self.last_price)

    def relative_volume(self, elapsed_fraction: float) -> float | None:
        if self.avg_daily_volume is None:
            return None
        return relative_volume(self.cumulative_volume, self.avg_daily_volume, elapsed_fraction)

    def opening_range(self, lookback_bars: int) -> tuple[float, float] | None:
        return opening_range(self.bars, lookback_bars)

    def atr(self, period: int = 14) -> float | None:
        return average_true_range(self.bars, period)

    def macd(self, fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[float, float] | None:
        return macd(self.bars, fast, slow, signal)


class BaseStrategy(ABC):
    """One instance per strategy type, shared across symbols. Per-symbol
    state (if any beyond SymbolContext) lives in `self._state[symbol]`,
    keyed by symbol, so a single strategy instance can track many symbols
    without cross-contamination."""

    name: str = "base"

    def __init__(self, config: Any):
        self.config = config
        self._state: dict[str, dict] = {}
        self.logger = logging.getLogger(f"warrior_bot.strategies.{self.name}")

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.config, "enabled", True))

    def state_for(self, symbol: str) -> dict:
        return self._state.setdefault(symbol, {})

    def reset_daily(self) -> None:
        self._state.clear()

    def already_triggered(self, ctx: SymbolContext, now: datetime) -> bool:
        """One entry per symbol per strategy per day is the intended
        discipline -- but only for a signal that actually became an order.

        A signal that was *rejected* by RiskManager (every current rejection
        reason is transient: all slots busy, reserved for a higher-ranked
        name, kill switch on, size rounded to zero) used to burn the symbol
        for the rest of the session anyway, because `triggered` was set
        inside evaluate() before risk ever saw it. On 2026-09-14 that
        silently discarded 96 setups -- every one of them a symbol this
        strategy could never look at again that day, however well it went on
        to trade. `rearm_after_rejection` schedules those to become eligible
        again after a cooldown; this check is what honours it."""
        state = self.state_for(ctx.symbol)
        if not state.get("triggered"):
            return False
        rearm_at = state.get("rearm_at")
        if rearm_at is not None and now >= rearm_at:
            state["triggered"] = False
            state.pop("rearm_at", None)
            return False
        return True

    def rearm_after_rejection(self, symbol: str, now: datetime, cooldown_seconds: float) -> None:
        """Makes a rejected symbol eligible to signal again once `cooldown`
        has passed. The cooldown (rather than re-arming instantly) is what
        keeps a level-triggered setup -- gap_and_go stays "broken out" for
        every bar after the breakout -- from re-firing on all of them while
        the book is full."""
        state = self.state_for(symbol)
        if state.get("triggered"):
            state["rearm_at"] = now + timedelta(seconds=cooldown_seconds)

    def _reject(self, ctx: SymbolContext, reason: str) -> None:
        """Breadcrumb for why a candidate didn't produce a signal on this
        bar -- silent at the default INFO log level, opt in via
        `logging.level: DEBUG` in config.yaml to see live gate misses.
        Only called for genuine criteria misses (a threshold or pattern
        check that failed), not for transient states like "not enough
        bars yet" or "already traded this symbol today"."""
        self.logger.debug("%s: no signal (%s)", ctx.symbol, reason)
        return None

    def _check_engaged(self, ctx: SymbolContext) -> bool:
        """Stock-level engagement gate, distinct from any per-signal MACD
        check: "should I even be looking at trades on this name right now."
        Re-evaluated on every bar from the current 9/20 EMA MACD only --
        a symbol that goes bearish and later turns bullish again is
        reconsidered, not excluded for the rest of the session. Uses
        (9, 20) specifically -- Ross Cameron's own chart MACD setup -- not
        the textbook (12, 26) used elsewhere."""
        macd_result = ctx.macd(fast=9, slow=20)
        if macd_result is not None and macd_result[0] <= macd_result[1]:
            return False
        return True

    @abstractmethod
    def evaluate(self, ctx: SymbolContext, now: datetime) -> Signal | None:
        """Called on every new bar for a symbol. Returns a Signal if the
        strategy's setup has just triggered, else None. Must not raise for
        "no signal" — only for genuine bugs."""
        raise NotImplementedError

    def _build_signal(
        self,
        ctx: SymbolContext,
        now: datetime,
        entry_price: float,
        stop_price: float,
        target_r_multiple: float,
        context: dict | None = None,
    ) -> Signal:
        risk_per_share = abs(entry_price - stop_price)
        target_price = entry_price + risk_per_share * target_r_multiple
        full_context = dict(context or {})
        if ctx.catalyst_category:
            full_context.setdefault("catalyst_category", ctx.catalyst_category)
            full_context.setdefault("catalyst_headline", ctx.catalyst_headline)
        if ctx.scanner_rank is not None:
            full_context.setdefault("scanner_rank", ctx.scanner_rank)
        return Signal(
            symbol=ctx.symbol,
            strategy=self.name,
            side="BUY",
            entry_price=entry_price,
            stop_price=stop_price,
            target_price=target_price,
            ts=now,
            context=full_context,
        )
