from __future__ import annotations

import logging

from ib_async import IB, Contract, RealTimeBarList, ScannerSubscription

from warrior_bot.config import AppConfig

logger = logging.getLogger("warrior_bot.broker.market_data")


def build_scanner_subscription(config: AppConfig) -> ScannerSubscription:
    s = config.scanner
    return ScannerSubscription(
        numberOfRows=s.max_candidates,
        instrument="STK",
        locationCode=s.location_code,
        scanCode=s.scan_code,
        abovePrice=s.above_price,
        belowPrice=s.below_price,
        aboveVolume=s.above_volume,
    )


async def scan_candidates(ib: IB, config: AppConfig) -> list[str]:
    """One-shot scan; returns a list of candidate symbols.

    Uses reqScannerData (request/response) rather than the long-lived
    reqScannerSubscription stream, since the strategy loop re-polls on
    `scanner.refresh_seconds` rather than reacting to push updates.
    """
    subscription = build_scanner_subscription(config)
    results = await ib.reqScannerDataAsync(subscription)
    symbols: list[str] = []
    for row in results:
        contract = row.contractDetails.contract
        if contract.symbol:
            symbols.append(contract.symbol)
    logger.info("Scanner returned %d candidates: %s", len(symbols), symbols)
    return symbols


def subscribe_real_time_bars(ib: IB, contract: Contract, use_rth: bool) -> RealTimeBarList:
    """5-second real-time bars, TRADES basis. Caller aggregates to 1-minute bars."""
    return ib.reqRealTimeBars(contract, 5, "TRADES", useRTH=use_rth)
