from __future__ import annotations

"""Lightweight sanity-check replay, NOT a validated backtester.

Intraday small-cap momentum is notoriously hard to backtest faithfully:
IBKR's historical data for illiquid small caps is thin/gappy at 1-minute
resolution, and fast breakouts don't have a realistic slippage/fill model
here. This tool replays historical bars through the exact same
pattern-detection functions used live, to catch obvious logic bugs (e.g. a
strategy that never fires, or fires on every bar) — it is not a substitute
for forward paper-trading, which is the primary validation method (see
scripts/daily_report.py and the trade journal).
"""

from datetime import datetime, timezone

from ib_async import IB

from warrior_bot.config import AppConfig
from warrior_bot.strategies.base_strategy import BaseStrategy, SymbolContext
from warrior_bot.strategies.indicators import Bar


async def replay_symbol(
    ib: IB,
    contract,
    strategies: list[BaseStrategy],
    config: AppConfig,
    duration: str = "1 D",
) -> list[dict]:
    bars = await ib.reqHistoricalDataAsync(
        contract,
        endDateTime="",
        durationStr=duration,
        barSizeSetting="1 min",
        whatToShow="TRADES",
        useRTH=config.trading.use_rth,
        formatDate=2,
        keepUpToDate=False,
    )

    ctx = SymbolContext(symbol=contract.symbol)
    signals_seen = []
    for ib_bar in bars:
        ctx.add_bar(Bar(time=ib_bar.date, open=ib_bar.open, high=ib_bar.high, low=ib_bar.low, close=ib_bar.close, volume=ib_bar.volume))
        now = ib_bar.date if isinstance(ib_bar.date, datetime) else datetime.now(timezone.utc)
        for strategy in strategies:
            signal = strategy.evaluate(ctx, now)
            if signal is not None:
                signals_seen.append({"strategy": strategy.name, "bar_time": now, "signal": signal})
    return signals_seen
