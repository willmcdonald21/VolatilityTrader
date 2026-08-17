from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from ib_async import IB, Contract

logger = logging.getLogger("warrior_bot.broker.news")


async def discover_provider_codes(ib: IB) -> str:
    """One-time lookup of which news providers this account is actually
    entitled to (varies by market-data subscription -- not knowable ahead
    of a live connection). Returns a '+'-joined code string ready to pass
    to reqHistoricalNews, or "" if the account has no news entitlements at
    all, in which case news fetching degrades to a silent no-op."""
    providers = await ib.reqNewsProvidersAsync()
    codes = "+".join(p.code for p in providers)
    if codes:
        logger.info("News providers available: %s", codes)
    else:
        logger.warning("No news providers entitled on this account -- catalyst detection will be a no-op")
    return codes


async def fetch_recent_headlines(
    ib: IB, contract: Contract, provider_codes: str, lookback_hours: int
) -> list[str]:
    """Headlines for `contract` in the trailing `lookback_hours`. Returns []
    (never raises for "no news") if there are no entitled providers or no
    matching articles -- absence of news is a normal, expected outcome."""
    if not provider_codes:
        return []
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=lookback_hours)
    articles = await ib.reqHistoricalNewsAsync(contract.conId, provider_codes, start, end, totalResults=20)
    if not articles:
        return []
    return [article.headline for article in articles]
