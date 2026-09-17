from __future__ import annotations

from datetime import datetime

from ib_async import IB, Contract

from warrior_bot.config import AppConfig
from warrior_bot.utils.time_utils import now_eastern


async def fetch_warmup_bars(ib: IB, contract: Contract, config: AppConfig, duration: str = "3600 S"):
    """1-minute bars covering the last hour (pre-market + open), used to seed
    VWAP/opening-range/relative-volume state before real-time bars take over.
    """
    return await ib.reqHistoricalDataAsync(
        contract,
        endDateTime="",
        durationStr=duration,
        barSizeSetting="1 min",
        whatToShow="TRADES",
        useRTH=config.trading.use_rth,
        formatDate=2,
        keepUpToDate=False,
    )


async def fetch_prior_close(ib: IB, contract: Contract) -> float | None:
    """Close of the last completed session before today.

    Selected by date rather than by position, because whether IBKR includes
    a bar for the current day depends on when this is called: during RTH
    today's forming bar is present (so the prior close is second-to-last),
    but pre-market -- when this bot does most of its trading -- it is not,
    and second-to-last is then the close from *two* sessions ago. Getting
    this wrong silently corrupts everything keyed off the prior close:
    gap_and_go's min_gap_pct gate and vwap_reversion's entire red-to-green
    setup, which uses it as the level being crossed.
    """
    bars = await ib.reqHistoricalDataAsync(
        contract,
        endDateTime="",
        durationStr="5 D",
        barSizeSetting="1 day",
        whatToShow="TRADES",
        useRTH=True,
        formatDate=2,
        keepUpToDate=False,
    )
    today = now_eastern().date()
    for bar in reversed(bars):
        bar_date = bar.date.date() if isinstance(bar.date, datetime) else bar.date
        if bar_date < today:
            return bar.close
    return None
