from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from warrior_bot.strategies.indicators import Bar, gap_pct, opening_range, relative_volume, vwap
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
        return Signal(
            symbol=ctx.symbol,
            strategy=self.name,
            side="BUY",
            entry_price=entry_price,
            stop_price=stop_price,
            target_price=target_price,
            ts=now,
            context=context or {},
        )
