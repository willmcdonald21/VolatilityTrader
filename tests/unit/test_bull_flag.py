from __future__ import annotations

from datetime import datetime

from tests.unit.fixtures import make_bars
from warrior_bot.config import BullFlagConfig
from warrior_bot.strategies.base_strategy import SymbolContext
from warrior_bot.strategies.bull_flag import BullFlagStrategy
from warrior_bot.utils.time_utils import EASTERN

NOW = datetime(2026, 1, 5, 10, 0, tzinfo=EASTERN)

PASSING_BARS = [
    (10.0, 10.0, 9.9, 10.0, 1000),    # baseline
    (10.0, 12.0, 10.0, 11.8, 3000),   # spike -- big move, heavy volume
    (11.8, 11.75, 11.6, 11.65, 300),  # consolidation 1 -- light volume
    (11.65, 11.7, 11.55, 11.6, 300),  # consolidation 2
    (11.6, 11.65, 11.5, 11.55, 300),  # consolidation 3
    (11.55, 12.2, 11.55, 12.2, 1000),  # breakout
]


def make_ctx(bar_specs, symbol="FLAG", avg_daily_volume=100):
    # avg_daily_volume defaults low enough that any of these fixtures'
    # cumulative volume clears the 5x relative-volume floor by a wide
    # margin -- tests that care about relative volume specifically pass
    # an inflated value to push it below threshold instead.
    ctx = SymbolContext(symbol=symbol)
    ctx.bars = make_bars(bar_specs)
    ctx.avg_daily_volume = avg_daily_volume
    return ctx


def test_flag_breakout_triggers_signal():
    ctx = make_ctx(PASSING_BARS)
    strategy = BullFlagStrategy(BullFlagConfig())
    signal = strategy.evaluate(ctx, NOW)
    assert signal is not None
    assert signal.strategy == "bull_flag"
    assert signal.entry_price == 12.2
    assert signal.stop_price < signal.entry_price


def test_no_signal_without_flag_breakout():
    bars = PASSING_BARS[:-1] + [(11.55, 11.7, 11.5, 11.5, 1000)]  # fails to clear flag high of 11.75
    ctx = make_ctx(bars)
    strategy = BullFlagStrategy(BullFlagConfig())
    assert strategy.evaluate(ctx, NOW) is None


def test_no_signal_when_spike_too_small():
    # baseline nearly equal to spike high -> spike_pct below threshold
    bars = [
        (10.9, 10.95, 10.95, 10.95, 1000),
        (10.95, 11.0, 10.95, 10.98, 2000),
        (10.98, 10.97, 10.9, 10.95, 500),
        (10.95, 10.96, 10.9, 10.94, 500),
        (10.94, 10.97, 10.9, 10.95, 500),
        (10.95, 11.05, 10.95, 11.05, 1000),
    ]
    ctx = make_ctx(bars)
    strategy = BullFlagStrategy(BullFlagConfig())
    assert strategy.evaluate(ctx, NOW) is None


def test_no_signal_when_pullback_too_deep():
    bars = [
        (10.0, 10.0, 9.9, 10.0, 1000),
        (10.0, 11.0, 10.0, 10.9, 2000),
        (10.9, 10.5, 9.6, 9.7, 500),
        (9.7, 9.8, 9.5, 9.6, 500),
        (9.6, 9.7, 9.5, 9.6, 500),
        (9.6, 11.2, 9.6, 11.2, 1000),
    ]
    ctx = make_ctx(bars)
    strategy = BullFlagStrategy(BullFlagConfig())
    assert strategy.evaluate(ctx, NOW) is None


SINGLE_BAR_PULLBACK = [
    (10.0, 10.0, 9.9, 10.0, 1000),     # baseline
    (10.0, 12.0, 10.0, 11.8, 3000),    # spike
    (11.8, 11.75, 11.5, 11.6, 300),    # single-bar pullback -- light volume
    (11.6, 12.2, 11.6, 12.2, 1000),    # breakout
]


def test_single_bar_pullback_allowed_by_default():
    # "1 or more red candles" per source material -- a single-bar micro
    # pullback is the ideal case, not something the default should reject.
    ctx = make_ctx(SINGLE_BAR_PULLBACK)
    strategy = BullFlagStrategy(BullFlagConfig())
    assert strategy.evaluate(ctx, NOW) is not None


def test_consolidation_shorter_than_configured_minimum_rejected():
    ctx = make_ctx(SINGLE_BAR_PULLBACK)
    strategy = BullFlagStrategy(BullFlagConfig(min_consolidation_bars=3))
    assert strategy.evaluate(ctx, NOW) is None


def test_no_signal_when_pullback_volume_not_lighter_than_spike():
    bars = [
        (10.0, 10.0, 9.9, 10.0, 1000),    # baseline
        (10.0, 11.0, 10.0, 10.9, 1000),   # spike -- up-move volume = 1000+1000 = 2000
        (10.9, 10.8, 10.6, 10.7, 5000),   # pullback volume (5000) >= up-move volume (2000)
        (10.7, 11.2, 10.7, 11.2, 1000),   # breakout
    ]
    ctx = make_ctx(bars)
    strategy = BullFlagStrategy(BullFlagConfig())
    assert strategy.evaluate(ctx, NOW) is None


def test_does_not_retrigger_same_symbol_same_day():
    ctx = make_ctx(PASSING_BARS)
    strategy = BullFlagStrategy(BullFlagConfig())
    assert strategy.evaluate(ctx, NOW) is not None
    assert strategy.evaluate(ctx, NOW) is None


def test_no_signal_when_relative_volume_too_low():
    # same price structure as PASSING_BARS, but a huge average makes the
    # day's actual volume look tiny by comparison -- Ross's stated hard
    # floor: "if it doesn't have at least 5x average volume, it's not
    # worth touching"
    ctx = make_ctx(PASSING_BARS, avg_daily_volume=10_000_000)
    strategy = BullFlagStrategy(BullFlagConfig())
    assert strategy.evaluate(ctx, NOW) is None
