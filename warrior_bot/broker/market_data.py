from __future__ import annotations

import asyncio
import logging

from ib_async import IB, ScannerSubscription

from warrior_bot.config import AppConfig

logger = logging.getLogger("warrior_bot.broker.market_data")


class ScannerRefused(RuntimeError):
    """IBKR rejected the scan outright -- this is NOT "no gainers today".

    Confirmed live 2026-09-28: a disconnect leaked all 10 of IBKR's
    per-connection scanner-subscription slots server side, after which every
    scan came back with error 322 ("Only 10 simultaneous API scanner
    subscriptions are allowed") and an empty result set. reqScannerDataAsync
    surfaces that as a perfectly ordinary empty list, so the bot logged
    `Scanner returned 0 candidates` at INFO roughly 490 times over six hours
    -- indistinguishable from a quiet tape -- and never self-healed, because
    the supervisor only relaunches on process exit and the bot never exits.

    The blast radius grew when the scanner-rank eligibility gate landed on
    2026-09-26: an empty scan clears every symbol's rank, and a cleared rank
    makes a symbol ineligible for any strategy, so a scanner failure now
    silently blacks out the entire strategy layer rather than merely
    degrading discovery.
    """


# IBKR error codes that mean "your scan request was refused", as opposed to
# "your scan ran and matched nothing".
_SCANNER_REFUSAL_CODES = {
    162,  # historical market data service error (scanner subscription cancelled//rejected)
    321,  # server error validating the request
    322,  # too many simultaneous API scanner subscriptions
    365,  # no scanner subscription found for ticker id
    366,  # no historical data query found for ticker id
}


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


async def scan_candidates(ib: IB, config: AppConfig, timeout: float = 30.0) -> list[str]:
    """One-shot scan; returns a list of candidate symbols.

    Uses reqScannerData (request/response) rather than the long-lived
    reqScannerSubscription stream, since the strategy loop re-polls on
    `scanner.refresh_seconds` rather than reacting to push updates.

    Owns its own timeout, and that is load-bearing -- do NOT wrap this call
    in asyncio.wait_for again. ib_async's reqScannerDataAsync is:

        dataList = self.reqScannerSubscription(...)
        future = self.wrapper.startReq(dataList.reqId, container=dataList)
        await future
        self.client.cancelScannerSubscription(dataList.reqId)   # <-- skipped

    Cancelling it from outside interrupts `await future`, so the cancel on
    the last line never runs and the subscription stays open server side.
    IBKR allows ten per connection. That is the mechanism behind the
    outage described in ScannerRefused above, and it recurred on
    2026-10-01: the scanner timed out repeatedly during overnight data-farm
    maintenance, burned all ten slots in about six minutes, and every scan
    for the next three and a half hours was refused with code 322. One
    symbol onboarded and no signals fired for the whole pre-market session.

    Subscribing explicitly and cancelling in `finally` makes the cleanup
    unconditional -- on timeout, on refusal, and on cancellation from above.
    """
    subscription = build_scanner_subscription(config)

    refusals: list[tuple[int, str]] = []

    def _capture_error(reqId, errorCode, errorString, contract) -> None:
        if errorCode in _SCANNER_REFUSAL_CODES:
            refusals.append((errorCode, errorString))

    ib.errorEvent += _capture_error
    data_list = ib.reqScannerSubscription(subscription)
    try:
        results = await asyncio.wait_for(
            ib.wrapper.startReq(data_list.reqId, container=data_list), timeout
        )
    finally:
        try:
            ib.cancelScannerSubscription(data_list)
        except Exception:  # pragma: no cover - defensive
            # Never let cleanup mask the original failure, but do say so:
            # a cancel that silently fails is how the slots leak.
            logger.warning("Could not cancel scanner subscription %s", data_list.reqId, exc_info=True)
        try:
            ib.errorEvent -= _capture_error
        except Exception:  # pragma: no cover - defensive
            logger.debug("Could not detach scanner error listener", exc_info=True)

    symbols: list[str] = []
    for row in results:
        contract = row.contractDetails.contract
        if contract.symbol:
            symbols.append(contract.symbol)

    if not symbols and refusals:
        code, message = refusals[-1]
        raise ScannerRefused(f"IBKR refused the scan (code {code}): {message}")

    logger.info("Scanner returned %d candidates: %s", len(symbols), symbols)
    return symbols
