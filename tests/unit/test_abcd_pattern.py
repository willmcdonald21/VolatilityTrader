from __future__ import annotations

from datetime import datetime

from tests.unit.fixtures import make_bars
from warrior_bot.config import AbcdConfig
from warrior_bot.strategies.abcd_pattern import AbcdStrategy
from warrior_bot.strategies.base_strategy import SymbolContext
from warrior_bot.utils.time_utils import EASTERN

NOW = datetime(2026, 1, 5, 10, 0, tzinfo=EASTERN)

PASSING_BARS = [
    (10.0, 10.0, 9.9, 10.0, 1000),     # A baseline
    (10.0, 12.0, 10.0, 11.8, 3000),    # B spike high -- heavy volume
    (11.8, 11.75, 11.4, 11.5, 300),    # C pullback -- light volume
    (11.5, 11.55, 11.4, 11.45, 300),   # continuing pullback
    (11.45, 12.2, 11.45, 12.2, 1000),  # D breakout above B
]


def make_ctx(bar_specs, symbol="ABCD", avg_daily_volume=100):
    # avg_daily_volume defaults low enough that any of these fixtures'
    # cumulative volume clears the 5x relative-volume floor by a wide
    # margin -- tests that care about relative volume specifically pass
    # an inflated value to push it below threshold instead.
    ctx = SymbolContext(symbol=symbol)
    ctx.bars = make_bars(bar_specs)
    ctx.avg_daily_volume = avg_daily_volume
    return ctx


def test_abcd_breakout_triggers_signal():
    ctx = make_ctx(PASSING_BARS)
    strategy = AbcdStrategy(AbcdConfig())
    signal = strategy.evaluate(ctx, NOW)
    assert signal is not None
    assert signal.strategy == "abcd"
    assert signal.entry_price == 12.2
    assert signal.stop_price < signal.entry_price


def test_no_signal_without_d_breakout():
    bars = PASSING_BARS[:-1] + [(11.45, 11.9, 11.45, 11.6, 1000)]  # doesn't clear B's high of 12.0
    ctx = make_ctx(bars)
    strategy = AbcdStrategy(AbcdConfig())
    assert strategy.evaluate(ctx, NOW) is None


def test_no_signal_when_ab_move_too_small():
    bars = [
        (10.9, 10.95, 10.85, 10.95, 1000),
        (10.95, 11.0, 10.95, 10.98, 2000),
        (10.98, 10.95, 10.9, 10.93, 500),
        (10.93, 10.94, 10.9, 10.92, 500),
        (10.92, 11.05, 10.92, 11.05, 1000),
    ]
    ctx = make_ctx(bars)
    strategy = AbcdStrategy(AbcdConfig())
    assert strategy.evaluate(ctx, NOW) is None


def test_no_signal_when_pullback_too_shallow():
    bars = [
        (10.0, 10.0, 9.9, 10.0, 1000),
        (10.0, 11.0, 10.0, 10.9, 2000),
        (10.9, 10.95, 10.95, 10.95, 500),  # barely pulls back at all
        (10.95, 10.96, 10.94, 10.95, 500),
        (10.95, 11.2, 10.95, 11.2, 1000),
    ]
    ctx = make_ctx(bars)
    strategy = AbcdStrategy(AbcdConfig())
    assert strategy.evaluate(ctx, NOW) is None


def test_no_signal_when_pullback_too_deep():
    bars = [
        (10.0, 10.0, 9.9, 10.0, 1000),
        (10.0, 11.0, 10.0, 10.9, 2000),
        (10.9, 10.2, 9.6, 9.7, 500),
        (9.7, 9.8, 9.5, 9.6, 500),
        (9.6, 11.2, 9.6, 11.2, 1000),
    ]
    ctx = make_ctx(bars)
    strategy = AbcdStrategy(AbcdConfig())
    assert strategy.evaluate(ctx, NOW) is None


def test_does_not_retrigger_same_symbol_same_day():
    ctx = make_ctx(PASSING_BARS)
    strategy = AbcdStrategy(AbcdConfig())
    assert strategy.evaluate(ctx, NOW) is not None
    assert strategy.evaluate(ctx, NOW) is None


def test_no_signal_when_breakout_candle_strength_below_default_threshold():
    bars = PASSING_BARS[:-1] + [(12.3, 12.6, 12.1, 12.15, 1000)]  # closes above B's high but red/weak-bodied
    ctx = make_ctx(bars)
    strategy = AbcdStrategy(AbcdConfig())
    assert strategy.evaluate(ctx, NOW) is None


def test_signal_when_breakout_candle_strength_gate_disabled():
    bars = PASSING_BARS[:-1] + [(12.3, 12.6, 12.1, 12.15, 1000)]
    ctx = make_ctx(bars)
    strategy = AbcdStrategy(AbcdConfig(min_breakout_candle_strength=-1.0))
    assert strategy.evaluate(ctx, NOW) is not None


def test_no_signal_when_relative_volume_too_low():
    ctx = make_ctx(PASSING_BARS, avg_daily_volume=10_000_000)
    strategy = AbcdStrategy(AbcdConfig())
    assert strategy.evaluate(ctx, NOW) is None


def test_no_signal_when_pullback_volume_not_lighter_than_up_move():
    # same price structure as PASSING_BARS (still a valid pattern by the
    # pre-existing gates) but with pullback volume bumped above the
    # up-move's 4000 -- isolates the new volume-profile gate specifically
    bars = [
        (10.0, 10.0, 9.9, 10.0, 1000),
        (10.0, 12.0, 10.0, 11.8, 3000),
        (11.8, 11.75, 11.4, 11.5, 3000),
        (11.5, 11.55, 11.4, 11.45, 3000),
        (11.45, 12.2, 11.45, 12.2, 1000),
    ]
    ctx = make_ctx(bars)
    strategy = AbcdStrategy(AbcdConfig())
    assert strategy.evaluate(ctx, NOW) is None
