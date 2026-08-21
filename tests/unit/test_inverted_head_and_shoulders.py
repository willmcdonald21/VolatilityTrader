from __future__ import annotations

from datetime import datetime

from tests.unit.fixtures import make_bars
from warrior_bot.config import InvertedHeadAndShouldersConfig
from warrior_bot.strategies.base_strategy import SymbolContext
from warrior_bot.strategies.inverted_head_and_shoulders import InvertedHeadAndShouldersStrategy
from warrior_bot.utils.time_utils import EASTERN

NOW = datetime(2026, 1, 5, 10, 0, tzinfo=EASTERN)

# left shoulder (low=9.0) -> peak1 (high=9.8) -> head (low=8.5, deepest) ->
# peak2 (high=9.7) -> right shoulder (low=9.1, close to left shoulder) ->
# breakout bar closing above the neckline (min(9.8, 9.7) = 9.7)
PASSING_BARS = [
    (9.2, 9.3, 9.0, 9.1, 2000),   # left shoulder
    (9.1, 9.8, 9.05, 9.7, 3000),  # peak 1
    (9.5, 9.6, 8.5, 8.7, 4000),   # head
    (8.8, 9.7, 9.3, 9.6, 3000),   # peak 2
    (9.5, 9.55, 9.1, 9.3, 2000),  # right shoulder
    (9.3, 9.9, 9.25, 9.85, 5000),  # breakout, closes above neckline (9.7)
]


def make_ctx(bar_specs, symbol="IHS", avg_daily_volume=100):
    ctx = SymbolContext(symbol=symbol)
    ctx.bars = make_bars(bar_specs)
    ctx.avg_daily_volume = avg_daily_volume
    return ctx


def test_neckline_breakout_triggers_signal():
    ctx = make_ctx(PASSING_BARS)
    strategy = InvertedHeadAndShouldersStrategy(InvertedHeadAndShouldersConfig())
    signal = strategy.evaluate(ctx, NOW)
    assert signal is not None
    assert signal.strategy == "inverted_head_and_shoulders"
    assert signal.entry_price == 9.85
    assert signal.context["head_low"] == 8.5
    assert signal.context["neckline"] == 9.7
    assert signal.stop_price < signal.entry_price


def test_no_signal_without_neckline_breakout():
    bars = PASSING_BARS[:-1] + [(9.3, 9.6, 9.25, 9.5, 5000)]  # closes below the 9.7 neckline
    ctx = make_ctx(bars)
    strategy = InvertedHeadAndShouldersStrategy(InvertedHeadAndShouldersConfig())
    assert strategy.evaluate(ctx, NOW) is None


def test_no_signal_when_shoulders_too_asymmetric():
    bars = [
        (9.2, 9.3, 9.0, 9.1, 2000),      # left shoulder, low=9.0
        (9.1, 9.8, 9.1, 9.7, 3000),      # peak 1
        (9.5, 9.6, 8.5, 8.7, 4000),      # head, low=8.5 (still deepest)
        (12.9, 13.5, 13.2, 13.4, 3000),  # peak 2 -- low kept above the right shoulder below
        (13.05, 13.1, 13.0, 13.05, 2000),  # right shoulder, low=13.0 -- wildly far from 9.0
        (13.0, 14.0, 12.9, 13.9, 5000),  # breakout (irrelevant, rejected before this)
    ]
    ctx = make_ctx(bars)
    strategy = InvertedHeadAndShouldersStrategy(InvertedHeadAndShouldersConfig())
    assert strategy.evaluate(ctx, NOW) is None


def test_no_signal_when_head_at_window_edge():
    # strictly increasing lows -- the deepest trough is the very first bar,
    # leaving no room for a left shoulder before it
    bars = [
        (8.1, 8.2, 8.0, 8.1, 1000),
        (8.6, 8.7, 8.5, 8.6, 1000),
        (9.1, 9.2, 9.0, 9.1, 1000),
        (9.6, 9.7, 9.5, 9.6, 1000),
        (10.1, 10.2, 10.0, 10.1, 1000),
        (10.2, 10.6, 10.15, 10.55, 2000),
    ]
    ctx = make_ctx(bars)
    strategy = InvertedHeadAndShouldersStrategy(InvertedHeadAndShouldersConfig())
    assert strategy.evaluate(ctx, NOW) is None


def test_no_signal_when_head_depth_too_shallow():
    bars = [
        (9.78, 9.80, 9.75, 9.76, 1000),  # left shoulder, low=9.75
        (9.77, 9.78, 9.76, 9.775, 1000),  # peak 1
        (9.72, 9.75, 9.70, 9.71, 1000),  # head, low=9.70 -- barely below the neckline
        (9.72, 9.78, 9.71, 9.77, 1000),  # peak 2
        (9.74, 9.76, 9.72, 9.73, 1000),  # right shoulder, low=9.72
        (9.75, 9.9, 9.74, 9.85, 2000),   # breakout (irrelevant, rejected before this)
    ]
    ctx = make_ctx(bars)
    strategy = InvertedHeadAndShouldersStrategy(InvertedHeadAndShouldersConfig())
    assert strategy.evaluate(ctx, NOW) is None


def test_no_signal_when_relative_volume_too_low():
    ctx = make_ctx(PASSING_BARS, avg_daily_volume=10_000_000)
    strategy = InvertedHeadAndShouldersStrategy(InvertedHeadAndShouldersConfig())
    assert strategy.evaluate(ctx, NOW) is None


def test_does_not_retrigger_same_symbol_same_day():
    ctx = make_ctx(PASSING_BARS)
    strategy = InvertedHeadAndShouldersStrategy(InvertedHeadAndShouldersConfig())
    assert strategy.evaluate(ctx, NOW) is not None
    assert strategy.evaluate(ctx, NOW) is None
