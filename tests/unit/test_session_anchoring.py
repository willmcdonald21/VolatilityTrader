"""Regression tests for the 2026-09-30 gate-correctness round.

Every test here pins a specific defect that was live in production, named in
the test's own comment. They are grouped by the thing that was wrong rather
than by module, because the defects were correlated: all five came from
indicators measuring from the wrong reference point.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tests.unit.fixtures import make_bars
from warrior_bot.broker.historical import warmup_duration
from warrior_bot.config import PullbackQualityConfig
from warrior_bot.strategies.base_strategy import SymbolContext
from warrior_bot.strategies.indicators import Bar, resample_bars
from warrior_bot.strategies.pullback_validity import validate_pullback
from warrior_bot.utils.time_utils import (
    EASTERN,
    session_anchor,
    session_elapsed_fraction,
)


def et(hour, minute=0, day=5):
    return datetime(2026, 1, day, hour, minute, tzinfo=EASTERN)


# --------------------------------------------------------------------------
# session_elapsed_fraction: the flat pre-market epsilon
# --------------------------------------------------------------------------


def test_premarket_minutes_are_not_all_graded_identically():
    # The defect: the old implementation returned a flat 0.01 for every
    # minute from midnight through 09:33 ET. 04:05 and 09:29 -- five and a
    # half hours of accumulating volume apart -- produced the same
    # relative-volume denominator.
    assert session_elapsed_fraction(et(4, 5)) < session_elapsed_fraction(et(6, 30))
    assert session_elapsed_fraction(et(6, 30)) < session_elapsed_fraction(et(9, 29))


def test_elapsed_fraction_is_monotonic_across_the_session():
    times = [et(4, 0), et(4, 30), et(6, 30), et(8, 0), et(9, 29), et(9, 31), et(12, 0), et(15, 55)]
    values = [session_elapsed_fraction(t) for t in times]
    assert values == sorted(values)
    assert all(0.0 < v <= 1.0 for v in values)


def test_min_rel_volume_means_the_same_thing_pre_market_and_midday():
    # The consequence of the flat epsilon: with min_rel_volume: 5.0 the
    # config number encoded "at least 5% of ADV" at 08:00 but "at least
    # 192% of ADV" at noon -- two completely different rules from one
    # setting, and the most likely reason this bot traded almost
    # exclusively pre-market. Pre-market should still be an easier bar to
    # clear than noon, but by a sane multiple rather than 38x.
    premarket = 5.0 * session_elapsed_fraction(et(8, 0))
    midday = 5.0 * session_elapsed_fraction(et(12, 0))
    assert 2.0 < midday / premarket < 8.0


def test_elapsed_fraction_normalizes_a_utc_argument():
    # 13:00 UTC is 08:00 ET. Passing UTC used to compare a UTC clock time
    # against ET session boundaries.
    utc = datetime(2026, 1, 5, 13, 0, tzinfo=timezone.utc)
    assert session_elapsed_fraction(utc) == pytest.approx(session_elapsed_fraction(et(8, 0)))


def test_elapsed_fraction_never_zero_at_the_premarket_open():
    # Divide-by-zero guard for relative_volume in the first seconds.
    assert session_elapsed_fraction(et(4, 0)) > 0.0


# --------------------------------------------------------------------------
# session_anchor + session-scoped VWAP / volume
# --------------------------------------------------------------------------


def test_anchor_is_todays_four_am():
    assert session_anchor(et(9, 30)) == et(4, 0)


def test_anchor_before_four_am_belongs_to_the_previous_session():
    assert session_anchor(et(2, 0, day=6)) == et(4, 0, day=5)


def test_session_volume_and_vwap_ignore_bars_before_the_anchor():
    # The defect: VWAP and cumulative volume were measured over every bar
    # held, which is a fixed warmup window measured from whenever the
    # scanner found the symbol. Two identical stocks got different VWAPs
    # based on discovery time, and the value re-anchored on every reconnect.
    anchor = datetime(2026, 1, 5, 4, 0, tzinfo=timezone.utc)
    bars = make_bars([(10, 10, 10, 10, 100)] * 6, start=anchor - timedelta(minutes=3))
    ctx = SymbolContext(symbol="T", bars=bars, session_anchor=anchor)
    assert ctx.cumulative_volume == 300  # 3 of the 6 bars, not all 6
    assert len(ctx.session_bars) == 3


def test_without_an_anchor_every_bar_counts():
    # The pre-2026-09-30 fallback, kept so tests and any caller that does
    # not set an anchor behave as before rather than silently seeing zero.
    bars = make_bars([(10, 10, 10, 10, 100)] * 6)
    ctx = SymbolContext(symbol="T", bars=bars)
    assert ctx.cumulative_volume == 600


def test_vwap_at_is_the_value_as_of_that_bar_not_now():
    # The defect behind the pullback checks below: ctx.vwap is the CURRENT
    # VWAP. On a rising stock it keeps climbing, so an earlier bar's
    # point-in-time VWAP is strictly lower.
    bars = make_bars([(10, 10, 10, 10, 100), (11, 11, 11, 11, 100), (12, 12, 12, 12, 100)])
    ctx = SymbolContext(symbol="T", bars=bars)
    assert ctx.vwap_at(0) == pytest.approx(10.0)
    assert ctx.vwap_at(2) == pytest.approx(11.0)
    assert ctx.vwap_at(2) == pytest.approx(ctx.vwap)
    assert ctx.vwap_at(99) is None


def test_adding_a_bar_invalidates_the_memo():
    bars = make_bars([(10, 10, 10, 10, 100)] * 3)
    ctx = SymbolContext(symbol="T", bars=list(bars))
    assert ctx.cumulative_volume == 300
    ctx.add_bar(Bar(time=bars[-1].time + timedelta(minutes=1), open=10, high=10, low=10, close=10, volume=50))
    assert ctx.cumulative_volume == 350


# --------------------------------------------------------------------------
# Point-in-time pullback level checks
# --------------------------------------------------------------------------


def _late_run_ctx():
    """An advance, a shallow pullback that holds VWAP *at the time*, then a
    heavy-volume run that drags session VWAP up past the pullback's lows.

    That last leg is the whole point: it is what makes the current VWAP a
    different number from the one the pullback actually faced.
    """
    advance = [(9.0 + i * 0.1, 9.05 + i * 0.1, 9.0 + i * 0.1, 9.05 + i * 0.1, 500) for i in range(10)]
    pullback = [(9.86, 9.89, 9.85, 9.88, 100)] * 3
    run = [(10.0 + i * 0.3, 10.1 + i * 0.3, 10.0 + i * 0.3, 10.1 + i * 0.3, 5000) for i in range(7)]
    return SymbolContext(symbol="T", bars=make_bars(advance + pullback + run))


_PERMISSIVE = PullbackQualityConfig(
    require_rising_volume_on_advance=False,
    require_pullback_lighter_than_prior_green_bar=False,
    require_5m_macd_confirmation=False,
    reject_5m_topping_tail=False,
)


def test_pullback_judged_against_the_vwap_it_actually_faced():
    # The defect: the VWAP check compared bars that closed minutes earlier
    # against VWAP as of now. On a rising stock -- every candidate this bot
    # looks at -- that is systematically too strict, rejecting pullbacks
    # that genuinely did hold VWAP when they printed.
    ctx = _late_run_ctx()
    pullback = ctx.bars[10:13]
    assert all(b.low < ctx.vwap for b in pullback), "fixture must be below the CURRENT vwap"
    assert all(b.low >= ctx.vwap_at(10 + i) for i, b in enumerate(pullback)), "but above it at the time"

    result = validate_pullback(
        pullback_bars=pullback, up_move_bars=ctx.bars[:10], ctx=ctx, config=_PERMISSIVE
    )
    assert result.reason != "pullback broke below VWAP"


def test_a_real_vwap_break_is_still_rejected():
    # The point-in-time fix must not disarm the gate: a bar that was below
    # VWAP at its own moment is still a break.
    ctx = _late_run_ctx()
    ctx.bars[12] = Bar(time=ctx.bars[12].time, open=9.0, high=9.0, low=5.0, close=9.0, volume=100)
    ctx._cache.clear()
    result = validate_pullback(
        pullback_bars=ctx.bars[10:13], up_move_bars=ctx.bars[:10], ctx=ctx, config=_PERMISSIVE
    )
    assert result.valid is False
    assert result.reason == "pullback broke below VWAP"


# --------------------------------------------------------------------------
# The advance, not the whole lookback window
# --------------------------------------------------------------------------


def test_rising_volume_is_measured_on_the_advance_not_preceding_chop():
    # The defect: callers pass everything from the start of a 40-bar window
    # up to the peak, so a symbol that chopped for half an hour before a
    # three-bar rip had ~37 bars of chop counted as its "advance".
    # has_rising_volume_on_advance was asked whether volume rose across the
    # chop -- it hadn't -- so a genuine advance was rejected.
    # Chop on steadily DECAYING volume, so the 15-bar window as a whole
    # reads as "volume declining" while the 3-bar advance inside it reads
    # as "volume rising" -- which is the distinction the gate is meant to
    # be making.
    chop = [(10.0, 10.0, 9.9, 10.0, 9000 - i * 700) for i in range(12)]
    advance = [(10.0, 10.3, 10.0, 10.3, 1000), (10.3, 10.7, 10.3, 10.7, 2000), (10.7, 11.2, 10.7, 11.2, 3000)]
    pullback = [(11.2, 11.2, 11.0, 11.05, 200)]
    ctx = SymbolContext(symbol="T", bars=make_bars(chop + advance + pullback))

    config = PullbackQualityConfig(
        require_rising_volume_on_advance=True,
        require_pullback_lighter_than_prior_green_bar=False,
        require_5m_macd_confirmation=False,
        reject_5m_topping_tail=False,
    )
    result = validate_pullback(
        pullback_bars=ctx.bars[15:], up_move_bars=ctx.bars[:15], ctx=ctx, config=config
    )
    assert result.reason != "volume declining on the preceding advance"


def test_an_advance_on_genuinely_declining_volume_is_still_rejected():
    # Trimming must not disarm the gate: the same shape, but with volume
    # falling across the advance itself, is the divergence the check exists
    # to catch.
    chop = [(10.0, 10.0, 9.9, 10.0, 500)] * 12
    advance = [(10.0, 10.3, 10.0, 10.3, 3000), (10.3, 10.7, 10.3, 10.7, 2000), (10.7, 11.2, 10.7, 11.2, 1000)]
    pullback = [(11.2, 11.2, 11.0, 11.05, 200)]
    ctx = SymbolContext(symbol="T", bars=make_bars(chop + advance + pullback))

    result = validate_pullback(
        pullback_bars=ctx.bars[15:],
        up_move_bars=ctx.bars[:15],
        ctx=ctx,
        config=PullbackQualityConfig(
            require_rising_volume_on_advance=True,
            require_pullback_lighter_than_prior_green_bar=False,
            require_5m_macd_confirmation=False,
            reject_5m_topping_tail=False,
        ),
    )
    assert result.valid is False
    assert result.reason == "volume declining on the preceding advance"


# --------------------------------------------------------------------------
# The still-forming 5-minute bucket
# --------------------------------------------------------------------------


def test_drop_partial_omits_the_still_forming_bucket():
    # The defect: the 5-minute topping-tail veto read five_min_bars[-1],
    # which one minute into a bucket is a 1-minute candle. An ordinary
    # 1-minute upper wick was vetoing entries as a "5-minute topping tail"
    # that the completed 5-minute candle would not have shown.
    start = datetime(2026, 1, 5, 9, 30, tzinfo=timezone.utc)
    bars = make_bars([(10, 10, 10, 10, 100)] * 6, start=start)  # 09:30-09:35
    assert len(resample_bars(bars, 5)) == 2                     # 09:30 bucket + forming 09:35
    assert len(resample_bars(bars, 5, drop_partial=True)) == 1  # only the complete one


def test_drop_partial_keeps_a_bucket_that_reached_its_final_minute():
    start = datetime(2026, 1, 5, 9, 30, tzinfo=timezone.utc)
    bars = make_bars([(10, 10, 10, 10, 100)] * 5, start=start)  # 09:30-09:34 inclusive
    assert len(resample_bars(bars, 5, drop_partial=True)) == 1


def test_drop_partial_tolerates_a_gap_in_a_thin_name():
    # A minute with no trades produces no bar, so a bucket-completeness test
    # based on counting bars would wrongly discard a finished bucket.
    start = datetime(2026, 1, 5, 9, 30, tzinfo=timezone.utc)
    bars = [
        Bar(time=start, open=10, high=10, low=10, close=10, volume=100),
        Bar(time=start + timedelta(minutes=4), open=10, high=10, low=10, close=10, volume=100),
    ]
    assert len(resample_bars(bars, 5, drop_partial=True)) == 1


# --------------------------------------------------------------------------
# Warmup window
# --------------------------------------------------------------------------


def test_warmup_covers_the_session_so_far():
    # The defect: a flat "3600 S". A symbol onboarded at 09:20 had five
    # hours of the pre-market session it was trading in simply missing.
    seconds = int(warmup_duration(et(9, 20)).split()[0])
    assert seconds == int((et(9, 20) - et(4, 0)).total_seconds())


def test_warmup_has_a_floor_just_after_the_open():
    assert warmup_duration(et(4, 10)) == "3600 S"


def test_warmup_is_capped_at_a_session():
    assert int(warmup_duration(et(19, 0)).split()[0]) <= 16 * 3600
