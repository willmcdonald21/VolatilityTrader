from __future__ import annotations

from tests.unit.fixtures import flat_bars, make_bars
from warrior_bot.strategies.indicators import (
    average_true_range,
    ema,
    gap_pct,
    is_high_volume_red_bar,
    is_lower_low,
    is_red_after_green,
    is_red_to_green,
    is_topping_tail,
    macd,
    opening_range,
    relative_volume,
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


def test_is_lower_low_true():
    prior = make_bars([(10.0, 10.5, 9.9, 10.2, 1000)])[0]
    current = make_bars([(10.2, 10.3, 9.7, 9.9, 1000)])[0]
    assert is_lower_low(current, prior) is True


def test_is_lower_low_false_when_low_holds():
    prior = make_bars([(10.0, 10.5, 9.9, 10.2, 1000)])[0]
    current = make_bars([(10.2, 10.3, 9.95, 10.1, 1000)])[0]
    assert is_lower_low(current, prior) is False
