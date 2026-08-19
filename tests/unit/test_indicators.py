from __future__ import annotations

from tests.unit.fixtures import flat_bars, make_bars
from warrior_bot.strategies.indicators import (
    average_true_range,
    candle_strength,
    ema,
    gap_pct,
    is_bottoming_tail,
    is_high_volume_red_bar,
    is_lower_low,
    is_momentum_exhausted,
    is_red_after_green,
    is_red_to_green,
    is_topping_tail,
    macd,
    opening_range,
    relative_volume,
    resample_bars,
    trailing_candidate,
    vwap,
)


def test_vwap_flat_price_equals_price():
    bars = make_bars([(10, 10, 10, 10, 100)] * 5)
    assert vwap(bars) == 10.0


def test_vwap_weights_by_volume():
    bars = make_bars([(10, 10, 10, 10, 100), (20, 20, 20, 20, 300)])
    result = vwap(bars)
    assert result == 17.5  # (10*100 + 20*300) / 400


def test_vwap_empty_returns_none():
    assert vwap([]) is None


def test_relative_volume_basic():
    assert relative_volume(volume_so_far=1000, avg_daily_volume=10000, elapsed_fraction=0.1) == 1.0


def test_relative_volume_zero_avg_returns_none():
    assert relative_volume(1000, 0, 0.5) is None


def test_opening_range_basic():
    bars = make_bars([(1, 5, 1, 3, 10), (1, 8, 2, 6, 10), (1, 4, 0.5, 2, 10)])
    high, low = opening_range(bars, lookback_bars=3)
    assert high == 8
    assert low == 0.5


def test_opening_range_respects_lookback():
    bars = make_bars([(1, 100, 1, 1, 1), (1, 2, 1, 1, 1), (1, 3, 1, 1, 1)])
    high, low = opening_range(bars, lookback_bars=2)
    assert high == 3  # excludes the first bar's high of 100


def test_gap_pct_positive():
    assert gap_pct(prior_close=10.0, current_price=12.0) == 20.0


def test_gap_pct_none_when_no_prior_close():
    assert gap_pct(None, 12.0) is None


def test_is_red_to_green_true_on_crossing_bar():
    assert is_red_to_green(prior_close=10.0, current_price=10.5, prior_price=9.5) is True


def test_is_red_to_green_false_if_already_green():
    assert is_red_to_green(prior_close=10.0, current_price=10.5, prior_price=10.2) is False


def test_is_red_to_green_false_if_still_red():
    assert is_red_to_green(prior_close=10.0, current_price=9.8, prior_price=9.5) is False


def test_average_true_range_flat_bars_is_zero():
    bars = make_bars([(10, 10, 10, 10, 100)] * 5)
    assert average_true_range(bars) == 0.0


def test_average_true_range_none_with_insufficient_bars():
    bars = make_bars([(10, 10, 10, 10, 100)])
    assert average_true_range(bars) is None


def test_ema_flat_price_equals_price():
    bars = make_bars([(10, 10, 10, 10, 100)] * 5)
    assert ema(bars, period=3) == 10.0


def test_ema_known_value():
    closes = [1, 2, 3, 4, 5]
    bars = make_bars([(c, c, c, c, 100) for c in closes])
    # seed = mean(1,2,3) = 2; multiplier = 2/(3+1) = 0.5
    # step 4: (4-2)*0.5+2 = 3; step 5: (5-3)*0.5+3 = 4
    assert ema(bars, period=3) == 4.0


def test_ema_none_with_insufficient_bars():
    bars = make_bars([(10, 10, 10, 10, 100)] * 2)
    assert ema(bars, period=3) is None


def test_trailing_candidate_ema_method_returns_ema_9():
    assert trailing_candidate(last_price=15.0, ema_9=12.5, atr=1.0, method="ema", atr_multiple=1.5) == 12.5


def test_trailing_candidate_atr_method_returns_price_minus_atr_multiple():
    result = trailing_candidate(last_price=15.0, ema_9=12.5, atr=2.0, method="atr", atr_multiple=1.5)
    assert result == 12.0  # 15.0 - 2.0*1.5


def test_trailing_candidate_atr_method_none_when_atr_missing():
    assert trailing_candidate(last_price=15.0, ema_9=12.5, atr=None, method="atr", atr_multiple=1.5) is None


def test_macd_none_with_insufficient_bars():
    bars = make_bars(flat_bars(10.0, 1000, 33))  # needs 34 for fast(12)+slow(26)+signal(9) warm-up
    assert macd(bars) is None


def test_macd_bullish_on_sustained_rise():
    closes = [15.0] * 20 + [15.0 + 0.5 * i for i in range(1, 15)]
    bars = make_bars([(c, c, c, c, 1000) for c in closes])
    result = macd(bars)
    assert result is not None
    macd_line, signal_line = result
    assert macd_line > signal_line


def test_macd_bearish_on_sustained_decline():
    closes = [15.0] * 20 + [15.0 - 0.5 * i for i in range(1, 15)]
    bars = make_bars([(c, c, c, c, 1000) for c in closes])
    result = macd(bars)
    assert result is not None
    macd_line, signal_line = result
    assert macd_line < signal_line


def test_is_topping_tail_true_for_long_upper_wick():
    bar = make_bars([(10.0, 11.0, 9.95, 10.05, 1000)])[0]  # tiny body, long upper wick
    assert is_topping_tail(bar) is True


def test_is_topping_tail_false_for_normal_candle():
    bar = make_bars([(10.0, 10.5, 9.8, 10.4, 1000)])[0]
    assert is_topping_tail(bar) is False


def test_is_topping_tail_false_for_doji_zero_body():
    bar = make_bars([(10.0, 11.0, 9.0, 10.0, 1000)])[0]
    assert is_topping_tail(bar) is False


def test_is_bottoming_tail_true_for_long_lower_wick():
    bar = make_bars([(10.05, 10.1, 9.0, 10.0, 1000)])[0]  # tiny body, long lower wick ("hammer")
    assert is_bottoming_tail(bar) is True


def test_is_bottoming_tail_true_regardless_of_red_or_green_body():
    # bullish "hammer" per the source material even when the small body
    # itself closed red, not just green
    red_hammer = make_bars([(10.05, 10.1, 9.0, 10.0, 1000)])[0]  # red: close < open
    green_hammer = make_bars([(10.0, 10.1, 9.0, 10.05, 1000)])[0]  # green: close > open
    assert is_bottoming_tail(red_hammer) is True
    assert is_bottoming_tail(green_hammer) is True


def test_is_bottoming_tail_false_for_normal_candle():
    bar = make_bars([(10.0, 10.5, 9.8, 10.4, 1000)])[0]
    assert is_bottoming_tail(bar) is False


def test_is_bottoming_tail_false_for_doji_zero_body():
    bar = make_bars([(10.0, 11.0, 9.0, 10.0, 1000)])[0]
    assert is_bottoming_tail(bar) is False


def test_is_bottoming_tail_false_when_upper_wick_dominates():
    bar = make_bars([(10.0, 11.0, 9.95, 10.05, 1000)])[0]  # this is the topping-tail fixture, mirror-checked
    assert is_bottoming_tail(bar) is False


def test_is_red_after_green_true():
    prior = make_bars([(10.0, 10.5, 9.9, 10.4, 1000)])[0]  # green
    current = make_bars([(10.4, 10.5, 10.0, 10.1, 1000)])[0]  # red
    assert is_red_after_green(prior, current) is True


def test_is_red_after_green_false_when_prior_is_red():
    prior = make_bars([(10.4, 10.5, 10.0, 10.1, 1000)])[0]  # red
    current = make_bars([(10.1, 10.2, 9.9, 10.0, 1000)])[0]  # red
    assert is_red_after_green(prior, current) is False


def test_is_high_volume_red_bar_true():
    bar = make_bars([(10.4, 10.5, 10.0, 10.1, 5000)])[0]  # red, high volume
    assert is_high_volume_red_bar(bar, avg_recent_volume=1000, multiple=2.0) is True


def test_is_high_volume_red_bar_false_when_not_red():
    bar = make_bars([(10.0, 10.5, 9.9, 10.4, 5000)])[0]  # green, high volume
    assert is_high_volume_red_bar(bar, avg_recent_volume=1000, multiple=2.0) is False


def test_is_high_volume_red_bar_false_when_volume_not_elevated():
    bar = make_bars([(10.4, 10.5, 10.0, 10.1, 1500)])[0]  # red, but not 2x the average
    assert is_high_volume_red_bar(bar, avg_recent_volume=1000, multiple=2.0) is False


EXHAUSTION_BARS_SHRINKING_BODY_AND_VOLUME = [
    (10.0, 10.95, 9.95, 10.9, 3000),   # body 0.9, volume 3000
    (10.9, 11.55, 10.85, 11.5, 2000),  # body 0.6, volume 2000
    (11.5, 11.85, 11.45, 11.8, 1000),  # body 0.3, volume 1000
]


def test_is_momentum_exhausted_true_when_body_and_volume_both_shrink():
    bars = make_bars(EXHAUSTION_BARS_SHRINKING_BODY_AND_VOLUME)
    assert is_momentum_exhausted(bars, lookback=3) is True


def test_is_momentum_exhausted_false_when_volume_still_rising():
    # same shrinking-body shape, but volume rising -- the source material's
    # explicit "weaker, lower-confidence" case, not treated as exhaustion
    bars = make_bars(
        [
            (10.0, 10.95, 9.95, 10.9, 1000),
            (10.9, 11.55, 10.85, 11.5, 2000),
            (11.5, 11.85, 11.45, 11.8, 3000),
        ]
    )
    assert is_momentum_exhausted(bars, lookback=3) is False


def test_is_momentum_exhausted_false_when_bodies_not_shrinking():
    bars = make_bars(
        [
            (10.0, 10.35, 9.95, 10.3, 3000),
            (10.3, 10.95, 10.25, 10.9, 2000),
            (10.9, 11.85, 10.85, 11.8, 1000),
        ]
    )
    assert is_momentum_exhausted(bars, lookback=3) is False


def test_is_momentum_exhausted_false_when_a_bar_is_red():
    bars = make_bars(
        [
            (10.0, 10.95, 9.95, 10.9, 3000),
            (11.5, 11.55, 10.85, 10.9, 2000),  # red: close < open
            (11.5, 11.85, 11.45, 11.8, 1000),
        ]
    )
    assert is_momentum_exhausted(bars, lookback=3) is False


def test_is_momentum_exhausted_false_with_insufficient_bars():
    bars = make_bars(EXHAUSTION_BARS_SHRINKING_BODY_AND_VOLUME[:2])
    assert is_momentum_exhausted(bars, lookback=3) is False


def test_is_momentum_exhausted_respects_custom_lookback():
    bars = make_bars(EXHAUSTION_BARS_SHRINKING_BODY_AND_VOLUME[:2])
    assert is_momentum_exhausted(bars, lookback=2) is True


def test_resample_bars_empty_returns_empty():
    assert resample_bars([], bucket_minutes=5) == []


def test_resample_bars_zero_bucket_returns_empty():
    bars = make_bars([(10.0, 10.5, 9.8, 10.2, 100)])
    assert resample_bars(bars, bucket_minutes=0) == []


def test_resample_bars_groups_into_5_minute_buckets():
    # 7 one-minute bars starting exactly on a 5-minute wall-clock boundary
    # (fixtures.BASE_TIME is 09:30 UTC) -> bucket 1 gets minutes :30-:34
    # (5 bars), bucket 2 gets :35-:36 (2 bars, a partial/forming bucket).
    bars = make_bars(
        [
            (10.0, 10.5, 9.8, 10.2, 100),
            (10.2, 10.6, 10.1, 10.4, 100),
            (10.4, 10.8, 10.3, 10.6, 100),
            (10.6, 10.7, 10.2, 10.3, 100),
            (10.3, 10.9, 10.3, 10.8, 100),
            (10.8, 11.0, 10.7, 10.9, 100),
            (10.9, 11.1, 10.8, 11.0, 100),
        ]
    )
    resampled = resample_bars(bars, bucket_minutes=5)
    assert len(resampled) == 2
    first, second = resampled
    assert first.open == 10.0
    assert first.high == 10.9
    assert first.low == 9.8
    assert first.close == 10.8
    assert first.volume == 500
    assert second.open == 10.8
    assert second.high == 11.1
    assert second.low == 10.7
    assert second.close == 11.0
    assert second.volume == 200


def test_candle_strength_full_bodied_green_is_plus_one():
    bar = make_bars([(10.0, 11.0, 10.0, 11.0, 1000)])[0]  # open=low, close=high
    assert candle_strength(bar) == 1.0


def test_candle_strength_full_bodied_red_is_minus_one():
    bar = make_bars([(11.0, 11.0, 10.0, 10.0, 1000)])[0]  # open=high, close=low
    assert candle_strength(bar) == -1.0


def test_candle_strength_green_with_large_upper_wick_is_weaker_than_full_bodied():
    bar = make_bars([(10.0, 11.0, 9.95, 10.05, 1000)])[0]  # closes green but barely, long upper wick
    assert 0 < candle_strength(bar) < 1.0


def test_candle_strength_red_with_large_lower_wick_is_less_bearish_than_full_bodied():
    bar = make_bars([(10.05, 10.05, 9.0, 9.95, 1000)])[0]  # closes red but barely, long lower wick
    assert -1.0 < candle_strength(bar) < 0


def test_candle_strength_zero_range_returns_zero():
    bar = make_bars([(10.0, 10.0, 10.0, 10.0, 1000)])[0]
    assert candle_strength(bar) == 0.0


def test_is_lower_low_true():
    prior = make_bars([(10.0, 10.5, 9.9, 10.2, 1000)])[0]
    current = make_bars([(10.2, 10.3, 9.7, 9.9, 1000)])[0]
    assert is_lower_low(current, prior) is True


def test_is_lower_low_false_when_low_holds():
    prior = make_bars([(10.0, 10.5, 9.9, 10.2, 1000)])[0]
    current = make_bars([(10.2, 10.3, 9.95, 10.1, 1000)])[0]
    assert is_lower_low(current, prior) is False
