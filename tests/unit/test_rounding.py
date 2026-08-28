from __future__ import annotations

from warrior_bot.utils.rounding import round_to_tick, tick_size


def test_tick_size_is_a_penny_at_or_above_one_dollar():
    assert tick_size(1.0) == 0.01
    assert tick_size(13.28) == 0.01


def test_tick_size_is_sub_penny_below_one_dollar():
    assert tick_size(0.9999) == 0.0001
    assert tick_size(0.05) == 0.0001


def test_rounds_journal_example_stop_limit_price():
    # the exact dirty value observed in the live bug (RAM stop-limit leg)
    assert round_to_tick(13.21683375) == 13.22


def test_rounds_sub_dollar_price_to_sub_penny_tick():
    assert round_to_tick(0.08341234) == 0.0834


def test_rounds_journal_example_target_price():
    assert round_to_tick(13.4751) == 13.48


def test_no_floating_point_residue():
    for value in (13.21683375, 0.08341234, 9.0 * 0.995, 1.00005, 0.08333333):
        result = round_to_tick(value)
        assert result == round(result, 4)


def test_idempotent():
    for value in (13.21683375, 0.08341234, 9.0 * 0.995, 8.91, 1.00005):
        once = round_to_tick(value)
        twice = round_to_tick(once)
        assert once == twice


def test_boundary_at_one_dollar_crossover():
    assert round_to_tick(0.9999) == 0.9999
    assert round_to_tick(1.00005) == 1.0
