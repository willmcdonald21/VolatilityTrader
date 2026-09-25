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
        avg_daily_volume=10_000,  # low enough that cumulative volume clears the 5x relative-volume floor
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
        avg_daily_volume=5_000,  # low enough that cumulative volume clears the 5x relative-volume floor
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


def test_vwap_bounce_skipped_when_relative_volume_too_low():
    ctx = make_ctx(
        bar_specs=[
            (10.0, 10.0, 10.0, 10.0, 10),
            (10.0, 10.0, 10.0, 10.0, 10),
            (10.0, 10.05, 9.9, 9.95, 10),
            (9.95, 10.3, 9.95, 10.3, 10),
        ],
        prior_close=5.0,
        avg_daily_volume=10_000_000,  # huge average vs. tiny actual volume
    )
    strategy = VwapReversionStrategy(VwapReversionConfig())
    assert strategy.evaluate(ctx, NOW) is None


_CALM_RED_TO_GREEN_BARS = [(9.5, 9.51, 9.49, 9.5, 1000)] * 15  # tight range -> tiny ATR, doesn't itself trip red-to-green


def test_no_signal_when_red_to_green_bar_too_extended():
    # Mirrors EDBL, 2026-09-22: the crossing bar itself was an already-
    # parabolic spike relative to recent (calm) volatility, then reversed
    # and hit the stop within minutes. Many calm bars first so the spike's
    # own true range doesn't dominate its own rolling ATR average.
    ctx = make_ctx(
        bar_specs=_CALM_RED_TO_GREEN_BARS + [(9.5, 11.0, 9.5, 11.0, 5000)],  # crossing bar rips far past prior_close
        prior_close=10.0,
        avg_daily_volume=10_000,
    )
    strategy = VwapReversionStrategy(VwapReversionConfig())
    assert strategy.evaluate(ctx, NOW) is None


def test_red_to_green_signal_when_extension_gate_loosened_enough():
    # Two independent gates now (see is_entry_too_extended) -- both must be
    # loosened for this bar (10% past prior_close) to get through.
    ctx = make_ctx(
        bar_specs=_CALM_RED_TO_GREEN_BARS + [(9.5, 11.0, 9.5, 11.0, 5000)],
        prior_close=10.0,
        avg_daily_volume=10_000,
    )
    strategy = VwapReversionStrategy(
        VwapReversionConfig(max_extension_atr_multiple=1000.0, max_extension_pct=1000.0)
    )
    signal = strategy.evaluate(ctx, NOW)
    assert signal is not None
    assert signal.context["setup"] == "red_to_green"


def test_no_red_to_green_signal_when_only_the_atr_gate_is_loosened():
    # Confirmed live, 2026-09-25: the ATR gate alone fired zero times ever
    # -- max_extension_pct is the gate that actually has to hold here.
    ctx = make_ctx(
        bar_specs=_CALM_RED_TO_GREEN_BARS + [(9.5, 11.0, 9.5, 11.0, 5000)],
        prior_close=10.0,
        avg_daily_volume=10_000,
    )
    strategy = VwapReversionStrategy(VwapReversionConfig(max_extension_atr_multiple=1000.0))  # pct left at default (3.0)
    assert strategy.evaluate(ctx, NOW) is None


_CALM_VWAP_BOUNCE_BARS = [(10.0, 10.01, 9.99, 10.0, 1000)] * 18  # tight range, holds VWAP near 10.0


def test_no_signal_when_vwap_bounce_bar_too_extended():
    # Mirrors DCOY, 2026-09-22: the bounce bar itself already ran far past
    # VWAP relative to recent volatility. Many calm bars first so VWAP
    # stays anchored near 10.0 (not dragged off by the bounce bar itself)
    # and the bounce's own true range doesn't dominate its own ATR average.
    ctx = make_ctx(
        bar_specs=_CALM_VWAP_BOUNCE_BARS
        + [
            (10.0, 10.05, 9.95, 9.98, 1000),  # dips to touch VWAP
            (9.98, 10.8, 9.98, 10.8, 1000),  # bounce bar rips far past VWAP
        ],
        prior_close=5.0,
        avg_daily_volume=5_000,
    )
    strategy = VwapReversionStrategy(VwapReversionConfig())
    assert strategy.evaluate(ctx, NOW) is None


def test_vwap_bounce_signal_when_extension_gate_loosened_enough():
    ctx = make_ctx(
        bar_specs=_CALM_VWAP_BOUNCE_BARS
        + [
            (10.0, 10.05, 9.95, 9.98, 1000),
            (9.98, 10.8, 9.98, 10.8, 1000),
        ],
        prior_close=5.0,
        avg_daily_volume=5_000,
    )
    strategy = VwapReversionStrategy(
        VwapReversionConfig(max_extension_atr_multiple=1000.0, max_extension_pct=1000.0)
    )
    signal = strategy.evaluate(ctx, NOW)
    assert signal is not None
    assert signal.context["setup"] == "vwap_bounce"


def test_no_vwap_bounce_signal_when_only_the_atr_gate_is_loosened():
    ctx = make_ctx(
        bar_specs=_CALM_VWAP_BOUNCE_BARS
        + [
            (10.0, 10.05, 9.95, 9.98, 1000),
            (9.98, 10.8, 9.98, 10.8, 1000),
        ],
        prior_close=5.0,
        avg_daily_volume=5_000,
    )
    strategy = VwapReversionStrategy(VwapReversionConfig(max_extension_atr_multiple=1000.0))  # pct left at default (3.0)
    assert strategy.evaluate(ctx, NOW) is None


def test_does_not_retrigger_same_symbol_same_day():
    bar_specs = [
        (9.5, 9.6, 9.4, 9.5, 1000),
        (9.5, 9.8, 9.4, 9.8, 1000),
        (9.8, 10.2, 9.8, 10.2, 5000),
    ]
    ctx = make_ctx(bar_specs, prior_close=10.0, avg_daily_volume=10_000)
    strategy = VwapReversionStrategy(VwapReversionConfig())
    assert strategy.evaluate(ctx, NOW) is not None
    assert strategy.evaluate(ctx, NOW) is None
