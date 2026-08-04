from __future__ import annotations

from datetime import datetime

from tests.unit.fixtures import make_bars
from warrior_bot.config import BullFlagConfig
from warrior_bot.strategies.base_strategy import SymbolContext
from warrior_bot.strategies.bull_flag import BullFlagStrategy
from warrior_bot.utils.time_utils import EASTERN

NOW = datetime(2026, 1, 5, 10, 0, tzinfo=EASTERN)

PASSING_BARS = [
    (10.0, 10.0, 9.9, 10.0, 1000),   # baseline
    (10.0, 11.0, 10.0, 10.9, 2000),  # spike
    (10.9, 10.8, 10.6, 10.7, 500),   # consolidation 1
    (10.7, 10.75, 10.65, 10.7, 500),  # consolidation 2
    (10.7, 10.8, 10.6, 10.75, 500),  # consolidation 3
    (10.75, 11.2, 10.75, 11.2, 1000),  # breakout
]


def make_ctx(bar_specs, symbol="FLAG"):
    ctx = SymbolContext(symbol=symbol)
    ctx.bars = make_bars(bar_specs)
    return ctx


def test_flag_breakout_triggers_signal():
    ctx = make_ctx(PASSING_BARS)
    strategy = BullFlagStrategy(BullFlagConfig())
    signal = strategy.evaluate(ctx, NOW)
    assert signal is not None
    assert signal.strategy == "bull_flag"
    assert signal.entry_price == 11.2
    assert signal.stop_price < signal.entry_price


def test_no_signal_without_flag_breakout():
    bars = PASSING_BARS[:-1] + [(10.75, 10.79, 10.7, 10.7, 1000)]  # fails to clear flag high of 10.8
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


def test_no_signal_when_consolidation_too_short():
    bars = [
        (10.0, 10.0, 9.9, 10.0, 1000),
        (10.0, 11.0, 10.0, 10.9, 2000),
        (10.9, 10.8, 10.6, 10.7, 500),  # only 1 consolidation bar
        (10.7, 11.2, 10.7, 11.2, 1000),
    ]
    ctx = make_ctx(bars)
    strategy = BullFlagStrategy(BullFlagConfig())
    assert strategy.evaluate(ctx, NOW) is None


def test_does_not_retrigger_same_symbol_same_day():
    ctx = make_ctx(PASSING_BARS)
    strategy = BullFlagStrategy(BullFlagConfig())
    assert strategy.evaluate(ctx, NOW) is not None
    assert strategy.evaluate(ctx, NOW) is None
