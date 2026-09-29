from __future__ import annotations

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


async def scan_candidates(ib: IB, config: AppConfig) -> list[str]:
    """One-shot scan; returns a list of candidate symbols.

    Uses reqScannerData (request/response) rather than the long-lived
    reqScannerSubscription stream, since the strategy loop re-polls on
    `scanner.refresh_seconds` rather than reacting to push updates.

    Raises ScannerRefused when IBKR rejected the request rather than
    returning no matches. The two are indistinguishable from the return
    value alone -- both are an empty list -- so errors raised during the
    call are captured and inspected. Only an EMPTY result paired with a
    refusal code counts: a scan that returned rows despite some incidental
    error is a successful scan.
    """
    subscription = build_scanner_subscription(config)

    refusals: list[tuple[int, str]] = []

    def _capture_error(reqId, errorCode, errorString, contract) -> None:
        if errorCode in _SCANNER_REFUSAL_CODES:
            refusals.append((errorCode, errorString))

    ib.errorEvent += _capture_error
    try:
        results = await ib.reqScannerDataAsync(subscription)
    finally:
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
