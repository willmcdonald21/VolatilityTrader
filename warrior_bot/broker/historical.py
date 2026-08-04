from __future__ import annotations

from ib_async import IB, Contract

from warrior_bot.config import AppConfig


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
    bars = await ib.reqHistoricalDataAsync(
        contract,
        endDateTime="",
        durationStr="2 D",
        barSizeSetting="1 day",
        whatToShow="TRADES",
        useRTH=True,
        formatDate=2,
        keepUpToDate=False,
    )
    if len(bars) >= 2:
        return bars[-2].close
    return None
