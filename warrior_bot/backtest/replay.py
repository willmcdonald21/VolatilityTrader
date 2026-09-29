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

READ NOT_MODELLED BEFORE TRUSTING ANY OUTPUT. Until 2026-09-28 this module
was worse than useless: it built a SymbolContext without `prior_close` or
`avg_daily_volume`, so `relative_volume()` returned None and EVERY strategy
rejected at its first gate. It returned an empty list unconditionally,
regardless of the code under test, and had no tests and no callers -- so
running it before a deploy produced a clean-looking result that meant
nothing at all.
"""

from datetime import datetime, timezone

from ib_async import IB

from warrior_bot.broker.historical import fetch_prior_close
from warrior_bot.config import AppConfig
from warrior_bot.strategies.base_strategy import BaseStrategy, SymbolContext
from warrior_bot.strategies.indicators import Bar

# Everything live does between "a strategy returned a Signal" and "money
# moved". Printed with every run so a replay result is never mistaken for
# a validated backtest. Each of these independently breaks predictiveness.
NOT_MODELLED = (
    "order fills of any kind -- a signal here is NOT a trade",
    "the limit-order adverse-selection problem: entries rest at the breakout bar's "
    "close, so a breakout that keeps running never fills, and you are preferentially "
    "filled on the ones that come back (i.e. the losers)",
    "the 5-minute unfilled-entry cancel (risk.entry_fill_timeout_seconds)",
    "partial fills (a single entry routinely arrives as 20+ fills)",
    "slippage and the stop-limit offset",
    "the conservative stop clamp (risk.max_stop_distance_pct), which rewrites "
    "stop/target/size AFTER the strategy builds the signal",
    "every RiskManager gate: kill switch, daily-loss halt, entry window, "
    "max_concurrent_positions, reserved top-tier slot, 2-lot cap, cross-strategy "
    "conflict, add-on delay",
    "position sizing -- there is no share count here, so no P&L",
    "the scanner-rank eligibility gate (scanner.max_eligible_rank)",
    "profit tiers, breakeven, ATR trailing, and reversal exits",
    "the 15:55 EOD flatten",
    "live bar timing: live evaluates the just-CLOSED bar (bars[-2]) roughly a "
    "minute after it opened, this replays with no such latency",
    "VWAP/relative-volume anchoring: live anchors both to a rolling 60-minute "
    "warmup window from onboarding time, this sees a contiguous session, so the "
    "ENTRY GATES THEMSELVES evaluate differently here than in production",
)


def describe_limitations() -> str:
    lines = ["This replay does NOT model:"] + [f"  - {item}" for item in NOT_MODELLED]
    return "\n".join(lines)


async def replay_symbol(
    ib: IB,
    contract,
    strategies: list[BaseStrategy],
    config: AppConfig,
    duration: str = "1 D",
    scanner_rank: int | None = 1,
) -> list[dict]:
    """Replays `duration` of 1-minute bars through `strategies`.

    The context is populated the way WarriorBot._onboard_symbol populates
    it -- prior close and 20-day average daily volume -- because without
    them every strategy short-circuits at its relative-volume or gap gate
    and the replay silently reports nothing.
    """
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

    ctx = SymbolContext(symbol=contract.symbol, scanner_rank=scanner_rank)
    ctx.prior_close = await fetch_prior_close(ib, contract)
    ctx.avg_daily_volume = await _fetch_avg_daily_volume(ib, contract)

    if ctx.prior_close is None or not ctx.avg_daily_volume:
        raise ValueError(
            f"Cannot replay {contract.symbol}: prior_close={ctx.prior_close} "
            f"avg_daily_volume={ctx.avg_daily_volume}. Every strategy would reject at its "
            "first gate and the run would report zero signals regardless of the code under test."
        )

    signals_seen = []
    for ib_bar in bars:
        ctx.add_bar(
            Bar(
                time=ib_bar.date,
                open=ib_bar.open,
                high=ib_bar.high,
                low=ib_bar.low,
                close=ib_bar.close,
                volume=ib_bar.volume,
            )
        )
        now = ib_bar.date if isinstance(ib_bar.date, datetime) else datetime.now(timezone.utc)
        for strategy in strategies:
            signal = strategy.evaluate(ctx, now)
            if signal is not None:
                signals_seen.append({"strategy": strategy.name, "bar_time": now, "signal": signal})
    return signals_seen


async def _fetch_avg_daily_volume(ib: IB, contract) -> float | None:
    """20-day average daily volume, matching _onboard_symbol's own fetch."""
    daily_bars = await ib.reqHistoricalDataAsync(
        contract,
        endDateTime="",
        durationStr="20 D",
        barSizeSetting="1 day",
        whatToShow="TRADES",
        useRTH=True,
        formatDate=2,
        keepUpToDate=False,
    )
    if not daily_bars:
        return None
    return sum(b.volume for b in daily_bars) / len(daily_bars)
