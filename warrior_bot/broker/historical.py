from __future__ import annotations

import logging
from datetime import datetime

from ib_async import IB, Contract

from warrior_bot.config import AppConfig
from warrior_bot.utils.time_utils import now_eastern, session_anchor

logger = logging.getLogger("warrior_bot.broker.historical")

# The pre-2026-09-30 fixed window, kept as the degraded fallback.
_FALLBACK_DURATION = "3600 S"

# A fixed hour was never enough to seed a session-anchored VWAP, but there is
# no point asking IBKR for more 1-minute bars than a session can contain
# either. 04:00 to 20:00 ET is 16 hours; the floor keeps a symbol onboarded a
# few minutes after the pre-market open from requesting a near-zero window.
_MIN_WARMUP_SECONDS = 3600
_MAX_WARMUP_SECONDS = 16 * 3600


def warmup_duration(now: datetime | None = None) -> str:
    """Seconds of 1-minute history needed to cover the session so far."""
    now = now or now_eastern()
    elapsed = int((now - session_anchor(now)).total_seconds())
    return f"{min(max(elapsed, _MIN_WARMUP_SECONDS), _MAX_WARMUP_SECONDS)} S"


async def fetch_warmup_bars(ib: IB, contract: Contract, config: AppConfig, duration: str | None = None):
    """1-minute bars covering the session so far, used to seed
    VWAP/opening-range/relative-volume state before real-time bars take over.

    Was a flat "3600 S". That hour is what made every session-scoped number
    depend on discovery time: a symbol onboarded at 09:20 had five hours of
    the pre-market session it was trading in simply missing from its VWAP and
    cumulative volume, and the 5-minute MACD veto -- which needs 26 five-
    minute bars, i.e. 130 minutes -- could not evaluate at all until roughly
    80 minutes after onboarding. Fetching from the session anchor means the
    indicators are correct from the first bar. Bars that precede the anchor
    are harmless: SymbolContext.session_bars filters them out of VWAP and
    session volume, while EMA/ATR/MACD legitimately want the extra depth.

    Falls back to the old one-hour window if the wider request fails. A
    widened durationStr is the kind of thing IBKR can refuse for reasons
    outside this bot's control (pacing, a contract with thin history, a
    data-farm hiccup), and the caller treats a warmup failure as "do not
    onboard this symbol" -- so without the fallback a refusal here takes
    the whole strategy layer offline rather than degrading one symbol's
    VWAP. A worse warmup beats no candidates.
    """
    requested = duration or warmup_duration()

    async def _request(dur: str):
        return await ib.reqHistoricalDataAsync(
            contract,
            endDateTime="",
            durationStr=dur,
            barSizeSetting="1 min",
            whatToShow="TRADES",
            useRTH=config.trading.use_rth,
            formatDate=2,
            keepUpToDate=False,
        )

    try:
        bars = await _request(requested)
    except Exception:
        if requested == _FALLBACK_DURATION:
            raise
        logger.warning(
            "Warmup request for %s bars failed for %s -- retrying with %s; this symbol's VWAP "
            "will be anchored short until the next re-onboard",
            requested,
            contract.symbol,
            _FALLBACK_DURATION,
        )
        return await _request(_FALLBACK_DURATION)

    if not bars and requested != _FALLBACK_DURATION:
        # An empty result is the other way IBKR refuses a window: no
        # exception, just nothing. Indistinguishable from a genuinely
        # untraded symbol here, so retry once rather than onboard blind.
        logger.warning(
            "Warmup request for %s bars returned nothing for %s -- retrying with %s",
            requested,
            contract.symbol,
            _FALLBACK_DURATION,
        )
        return await _request(_FALLBACK_DURATION)
    return bars


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
