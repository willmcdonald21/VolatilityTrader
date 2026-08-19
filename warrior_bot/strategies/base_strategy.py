from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
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

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.config, "enabled", True))

    def state_for(self, symbol: str) -> dict:
        return self._state.setdefault(symbol, {})

    def reset_daily(self) -> None:
        self._state.clear()

    def _check_engaged(self, ctx: SymbolContext) -> bool:
        """Stock-level engagement gate, distinct from any per-signal MACD
        check: once the 9/20 EMA MACD crosses bearish for a symbol, this
        strategy permanently stops considering new entries on it for the
        rest of the session ("should I even be looking at trades on this
        name right now"), until reset_daily(). Uses (9, 20) specifically --
        Ross Cameron's own chart MACD setup -- not the textbook (12, 26)
        used elsewhere."""
        state = self.state_for(ctx.symbol)
        if state.get("disengaged"):
            return False
        macd_result = ctx.macd(fast=9, slow=20)
        if macd_result is not None and macd_result[0] <= macd_result[1]:
            state["disengaged"] = True
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
