from __future__ import annotations

from datetime import datetime, time, timedelta
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


def session_anchor(now: datetime | None = None) -> datetime:
    """Start of the trading session this ET calendar date belongs to:
    today's 04:00 pre-market open, always.

    Deliberately NOT "yesterday's 04:00, if called before 04:00". Three
    reasons, all specific to this bot:

      - reset_daily_state() clears contexts on the ET calendar date
        change, i.e. at midnight, not at 04:00. A symbol onboarded at
        02:00 with yesterday's anchor would keep it for the whole of
        today's session, so its VWAP would fold in all of yesterday.
      - session_elapsed_fraction already treats 00:00-04:00 as "before
        this session started". Returning yesterday's anchor here made the
        two functions disagree in exactly that window -- a VWAP measured
        over yesterday's session divided against today's expected volume.
      - the bot flattens at 15:55 and takes no entry before 06:30, so it
        has no interest in the 20:00-04:00 overnight tape anyway.

    Between midnight and 04:00 the anchor is therefore in the future and
    session_bars is empty, which reads correctly as "this session has not
    started": relative volume is 0 and nothing signals. Bars accumulate
    normally from 04:00.

    This is the reference point VWAP and session volume are measured from.
    Before this existed, both were measured from whenever the scanner
    happened to discover a symbol -- a fixed 60-minute warmup window -- so
    two identical stocks got different VWAPs based on discovery time, the
    value never re-anchored at the open, and it silently re-anchored again
    on every reconnect. 04:00 rather than 09:30 because this bot trades
    pre-market from 06:30, and a VWAP that ignores the pre-market session
    it is trading in would be meaningless there.
    """
    now = to_eastern(now) if now is not None else now_eastern()
    return now.replace(hour=PRE_MARKET_OPEN.hour, minute=PRE_MARKET_OPEN.minute, second=0, microsecond=0)


# Share of a typical symbol's daily volume that trades in the 04:00-09:30
# pre-market window. Gives pre-market a real elapsed curve instead of a flat
# epsilon. Deliberately approximate -- the point is that 04:05 and 09:29 stop
# being graded identically, not that this is precisely calibrated. Settable
# from config (session.premarket_volume_share) via set_premarket_volume_share,
# since config.yaml's own rule is that nothing thresholded is hard-coded.
PREMARKET_VOLUME_SHARE = 0.10


def set_premarket_volume_share(share: float) -> None:
    """Overrides the pre-market volume share from config, once at startup."""
    global PREMARKET_VOLUME_SHARE
    PREMARKET_VOLUME_SHARE = share


def session_elapsed_fraction(now: datetime | None = None) -> float:
    """Fraction of a day's expected volume that should have traded by `now`.

    Used to scale a 20-day average daily volume down to an
    expected-volume-by-now baseline for relative volume.

    Pre-market is modelled explicitly rather than clamped. The old version
    returned a flat 0.01 for every minute from midnight through 09:33 ET,
    which had two consequences: 04:05 and 09:29 were graded identically
    (five and a half hours of accumulating volume treated as the same
    elapsed time), and the resulting relative-volume number could not be
    compared across times of day at all. With min_rel_volume: 5.0, that
    meant "at least 5% of average daily volume" at 08:00 but "at least 192%"
    at noon -- the same config number encoding two completely different
    rules, and the most likely reason this bot traded almost exclusively
    pre-market.

    Accepts `now` in any timezone (or naive, treated as Eastern) and
    normalizes to Eastern before comparing against session boundaries.
    """
    now = to_eastern(now) if now is not None else now_eastern()
    premarket_start = now.replace(
        hour=PRE_MARKET_OPEN.hour, minute=PRE_MARKET_OPEN.minute, second=0, microsecond=0
    )
    rth_start = now.replace(hour=RTH_OPEN.hour, minute=RTH_OPEN.minute, second=0, microsecond=0)
    rth_end = now.replace(hour=RTH_CLOSE.hour, minute=RTH_CLOSE.minute, second=0, microsecond=0)

    if now <= premarket_start:
        return _MIN_ELAPSED_FRACTION
    if now < rth_start:
        # Pre-market: ramp linearly through PREMARKET_VOLUME_SHARE.
        through = (now - premarket_start).total_seconds() / (rth_start - premarket_start).total_seconds()
        return max(_MIN_ELAPSED_FRACTION, through * PREMARKET_VOLUME_SHARE)
    if now >= rth_end:
        return 1.0
    # Regular hours: the remaining share, spread across the RTH session.
    through_rth = (now - rth_start).total_seconds() / (rth_end - rth_start).total_seconds()
    return PREMARKET_VOLUME_SHARE + through_rth * (1.0 - PREMARKET_VOLUME_SHARE)


# Floor, so relative volume can never divide by zero in the first seconds
# after the pre-market open.
_MIN_ELAPSED_FRACTION = 0.001


def is_pre_market(now: datetime | None = None) -> bool:
    now = to_eastern(now) if now is not None else now_eastern()
    return PRE_MARKET_OPEN <= now.time() < RTH_OPEN


def is_regular_hours(now: datetime | None = None) -> bool:
    now = to_eastern(now) if now is not None else now_eastern()
    return RTH_OPEN <= now.time() < RTH_CLOSE


def is_active_session(now: datetime | None = None) -> bool:
    """True from pre-market open through after-hours close (4:00-20:00 ET)
    -- the bot's full active window. Used to gate work that should pause
    overnight (e.g. the live-data staleness watchdog in main.py) rather than
    needlessly resubscribing/churning every tracked symbol once there's no
    session running at all."""
    now = to_eastern(now) if now is not None else now_eastern()
    return PRE_MARKET_OPEN <= now.time() < AFTER_HOURS_CLOSE


def session_date_start(now: datetime | None = None) -> datetime:
    """Midnight ET on the current ET date.

    The lone function here that did not normalise its argument to Eastern:
    given a UTC datetime it returned midnight UTC, which after 19:00/20:00
    ET is the WRONG DAY. Latent only because its sole caller passes no
    argument -- but it feeds account_state's daily_realized_pnl, which arms
    the daily-loss halt, so a future caller passing UTC would have moved
    that boundary by 4-5 hours silently."""
    now = to_eastern(now) if now is not None else now_eastern()
    return now.replace(hour=0, minute=0, second=0, microsecond=0)
