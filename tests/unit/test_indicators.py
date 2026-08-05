from __future__ import annotations

from tests.unit.fixtures import make_bars
from warrior_bot.strategies.indicators import (
    average_true_range,
    ema,
    gap_pct,
    is_red_to_green,
    opening_range,
    relative_volume,
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
