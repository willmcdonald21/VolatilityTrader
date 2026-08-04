from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")

PRE_MARKET_OPEN = time(4, 0)
RTH_OPEN = time(9, 30)
RTH_CLOSE = time(16, 0)
AFTER_HOURS_CLOSE = time(20, 0)


def now_eastern() -> datetime:
    return datetime.now(EASTERN)


def to_eastern(dt: datetime) -> datetime:
    """Converts an aware datetime to Eastern; treats a naive datetime as
    already being Eastern (rather than guessing the system's local zone)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=EASTERN)
    return dt.astimezone(EASTERN)


def session_elapsed_fraction(now: datetime | None = None) -> float:
    """Fraction of the regular trading session (9:30-16:00 ET) elapsed.

    Clamped to [small epsilon, 1.0]. Used to scale a 20-day average daily
    volume down to an expected-volume-by-now baseline for relative-volume
    calculations. Pre-market minutes count as "0 elapsed" of RTH volume,
    since Warrior-Trading-style relative volume is conventionally measured
    against RTH averages even when the signal itself fires pre-market.

    Accepts `now` in any timezone (or naive, treated as Eastern) and
    normalizes to Eastern before comparing against session boundaries —
    callers elsewhere in the bot pass UTC-aware datetimes.
    """
    now = to_eastern(now) if now is not None else now_eastern()
    session_start = now.replace(hour=RTH_OPEN.hour, minute=RTH_OPEN.minute, second=0, microsecond=0)
    session_end = now.replace(hour=RTH_CLOSE.hour, minute=RTH_CLOSE.minute, second=0, microsecond=0)
    if now <= session_start:
        return 0.01
    if now >= session_end:
        return 1.0
    elapsed = (now - session_start).total_seconds()
    total = (session_end - session_start).total_seconds()
    return max(0.01, elapsed / total)


def is_pre_market(now: datetime | None = None) -> bool:
    now = to_eastern(now) if now is not None else now_eastern()
    return PRE_MARKET_OPEN <= now.time() < RTH_OPEN


def is_regular_hours(now: datetime | None = None) -> bool:
    now = to_eastern(now) if now is not None else now_eastern()
    return RTH_OPEN <= now.time() < RTH_CLOSE


def session_date_start(now: datetime | None = None) -> datetime:
    now = now or now_eastern()
    return now.replace(hour=0, minute=0, second=0, microsecond=0)
