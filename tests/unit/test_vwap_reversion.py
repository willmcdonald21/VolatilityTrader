from __future__ import annotations

from datetime import datetime

from tests.unit.fixtures import make_bars
from warrior_bot.config import VwapReversionConfig
from warrior_bot.strategies.base_strategy import SymbolContext
from warrior_bot.strategies.vwap_reversion import VwapReversionStrategy
from warrior_bot.utils.time_utils import EASTERN

NOW = datetime(2026, 1, 5, 10, 0, tzinfo=EASTERN)  # 30 min into RTH


def make_ctx(bar_specs, prior_close, avg_daily_volume, symbol="VWAP"):
    ctx = SymbolContext(symbol=symbol)
    ctx.bars = make_bars(bar_specs)
    ctx.prior_close = prior_close
    ctx.avg_daily_volume = avg_daily_volume
    return ctx


def test_red_to_green_triggers_signal():
    ctx = make_ctx(
        bar_specs=[
            (9.5, 9.6, 9.4, 9.5, 1000),
            (9.5, 9.8, 9.4, 9.8, 1000),   # prev bar: still red (9.8 < prior_close 10.0)
            (9.8, 10.2, 9.8, 10.2, 5000),  # current bar: crosses to green
        ],
        prior_close=10.0,
        avg_daily_volume=20_000,
    )
    strategy = VwapReversionStrategy(VwapReversionConfig())
    signal = strategy.evaluate(ctx, NOW)
    assert signal is not None
    assert signal.context["setup"] == "red_to_green"
    assert signal.entry_price == 10.2


def test_red_to_green_skipped_when_relative_volume_too_low():
    ctx = make_ctx(
        bar_specs=[
            (9.5, 9.6, 9.4, 9.5, 10),
            (9.5, 9.8, 9.4, 9.8, 10),
            (9.8, 10.2, 9.8, 10.2, 10),
        ],
        prior_close=10.0,
        avg_daily_volume=10_000_000,  # huge average vs. tiny actual volume
    )
    strategy = VwapReversionStrategy(VwapReversionConfig())
    assert strategy.evaluate(ctx, NOW) is None


def test_vwap_bounce_triggers_signal():
    ctx = make_ctx(
        bar_specs=[
            (10.0, 10.0, 10.0, 10.0, 1000),
            (10.0, 10.0, 10.0, 10.0, 1000),
            (10.0, 10.05, 9.9, 9.95, 1000),   # dips to touch VWAP
            (9.95, 10.3, 9.95, 10.3, 1000),   # bounces back above prior high and VWAP
        ],
        prior_close=5.0,  # far below everything -> red_to_green never applies
        avg_daily_volume=20_000,
    )
    strategy = VwapReversionStrategy(VwapReversionConfig())
    signal = strategy.evaluate(ctx, NOW)
    assert signal is not None
    assert signal.context["setup"] == "vwap_bounce"
    assert signal.entry_price == 10.3


def test_no_signal_when_pullback_too_far_from_vwap():
    ctx = make_ctx(
        bar_specs=[
            (10.0, 10.0, 10.0, 10.0, 1000),
            (10.0, 10.0, 10.0, 10.0, 1000),
            (10.0, 10.05, 9.5, 9.6, 1000),   # dips well past VWAP, not a tight touch
            (9.6, 10.3, 9.6, 10.3, 1000),
        ],
        prior_close=5.0,
        avg_daily_volume=20_000,
    )
    strategy = VwapReversionStrategy(VwapReversionConfig())
    assert strategy.evaluate(ctx, NOW) is None


def test_does_not_retrigger_same_symbol_same_day():
    bar_specs = [
        (9.5, 9.6, 9.4, 9.5, 1000),
        (9.5, 9.8, 9.4, 9.8, 1000),
        (9.8, 10.2, 9.8, 10.2, 5000),
    ]
    ctx = make_ctx(bar_specs, prior_close=10.0, avg_daily_volume=20_000)
    strategy = VwapReversionStrategy(VwapReversionConfig())
    assert strategy.evaluate(ctx, NOW) is not None
    assert strategy.evaluate(ctx, NOW) is None
