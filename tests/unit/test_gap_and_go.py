from __future__ import annotations

from datetime import datetime

from tests.unit.fixtures import make_bars
from warrior_bot.config import GapAndGoConfig
from warrior_bot.scanner.float_provider import FloatProvider
from warrior_bot.strategies.base_strategy import SymbolContext
from warrior_bot.strategies.gap_and_go import GapAndGoStrategy
from warrior_bot.utils.time_utils import EASTERN

NOW = datetime(2026, 1, 5, 9, 35, tzinfo=EASTERN)  # ~5 min into RTH


def make_ctx(bar_specs, prior_close=5.0, avg_daily_volume=10_000, symbol="GOGO"):
    ctx = SymbolContext(symbol=symbol)
    ctx.bars = make_bars(bar_specs)
    ctx.prior_close = prior_close
    ctx.avg_daily_volume = avg_daily_volume
    return ctx


def default_config(**overrides) -> GapAndGoConfig:
    return GapAndGoConfig(**overrides)


def test_breakout_triggers_signal():
    ctx = make_ctx(
        [
            (5.5, 5.7, 5.4, 5.6, 1000),
            (5.6, 5.8, 5.5, 5.7, 1000),
            (5.7, 5.9, 5.6, 5.75, 1000),
            (5.75, 5.85, 5.7, 5.8, 1000),
            (5.8, 6.5, 5.8, 6.5, 1000),  # breakout bar
        ]
    )
    strategy = GapAndGoStrategy(default_config())
    signal = strategy.evaluate(ctx, NOW)
    assert signal is not None
    assert signal.symbol == "GOGO"
    assert signal.strategy == "gap_and_go"
    assert signal.entry_price == 6.5
    assert signal.stop_price < signal.entry_price
    assert signal.target_price > signal.entry_price


def test_no_signal_without_breakout():
    ctx = make_ctx(
        [
            (5.5, 5.7, 5.4, 5.6, 1000),
            (5.6, 5.8, 5.5, 5.7, 1000),
            (5.7, 5.9, 5.6, 5.75, 1000),
            (5.75, 5.85, 5.7, 5.8, 1000),
            (5.8, 5.85, 5.75, 5.8, 1000),  # does not exceed prior high of 5.9
        ]
    )
    strategy = GapAndGoStrategy(default_config())
    assert strategy.evaluate(ctx, NOW) is None


def test_no_signal_when_gap_too_small():
    ctx = make_ctx(
        [
            (5.5, 5.7, 5.4, 5.6, 1000),
            (5.6, 5.8, 5.5, 5.7, 1000),
            (5.7, 5.9, 5.6, 5.75, 1000),
            (5.75, 5.85, 5.7, 5.8, 1000),
            (5.8, 5.95, 5.8, 5.95, 1000),
        ],
        prior_close=5.7,  # gap from 5.7 to 5.95 close is < 10%
    )
    strategy = GapAndGoStrategy(default_config())
    assert strategy.evaluate(ctx, NOW) is None


def test_no_signal_outside_price_band():
    ctx = make_ctx(
        [
            (25.0, 25.5, 24.5, 25.2, 1000),
            (25.2, 25.8, 25.1, 25.6, 1000),
            (25.6, 26.0, 25.5, 25.8, 1000),
            (25.8, 26.2, 25.7, 26.0, 1000),
            (26.0, 30.0, 26.0, 30.0, 1000),
        ],
        prior_close=20.0,
    )
    strategy = GapAndGoStrategy(default_config())
    assert strategy.evaluate(ctx, NOW) is None


def test_no_signal_when_relative_volume_too_low():
    ctx = make_ctx(
        [
            (5.5, 5.7, 5.4, 5.6, 10),
            (5.6, 5.8, 5.5, 5.7, 10),
            (5.7, 5.9, 5.6, 5.75, 10),
            (5.75, 5.85, 5.7, 5.8, 10),
            (5.8, 6.5, 5.8, 6.5, 10),
        ],
        avg_daily_volume=10_000_000,  # tiny volume so far vs. a huge average -> low rel volume
    )
    strategy = GapAndGoStrategy(default_config())
    assert strategy.evaluate(ctx, NOW) is None


def test_does_not_retrigger_same_symbol_same_day():
    ctx = make_ctx(
        [
            (5.5, 5.7, 5.4, 5.6, 1000),
            (5.6, 5.8, 5.5, 5.7, 1000),
            (5.7, 5.9, 5.6, 5.75, 1000),
            (5.75, 5.85, 5.7, 5.8, 1000),
            (5.8, 6.5, 5.8, 6.5, 1000),
        ]
    )
    strategy = GapAndGoStrategy(default_config())
    first = strategy.evaluate(ctx, NOW)
    assert first is not None
    second = strategy.evaluate(ctx, NOW)
    assert second is None


BREAKOUT_BARS = [
    (5.5, 5.7, 5.4, 5.6, 1000),
    (5.6, 5.8, 5.5, 5.7, 1000),
    (5.7, 5.9, 5.6, 5.75, 1000),
    (5.75, 5.85, 5.7, 5.8, 1000),
    (5.8, 6.5, 5.8, 6.5, 1000),  # breakout bar
]


def write_float_list(tmp_path, rows):
    path = tmp_path / "float_list.csv"
    lines = ["symbol,float_shares,updated_at"]
    for symbol, float_shares, updated_at in rows:
        lines.append(f"{symbol},{float_shares},{updated_at}")
    path.write_text("\n".join(lines) + "\n")
    return path


def test_float_filter_rejects_symbol_over_max(tmp_path):
    csv_path = write_float_list(tmp_path, [("GOGO", 30_000_000, datetime.now().date().isoformat())])
    ctx = make_ctx(BREAKOUT_BARS)
    strategy = GapAndGoStrategy(
        default_config(enable_float_filter=True, max_float_shares=10_000_000),
        float_provider=FloatProvider(csv_path),
    )
    assert strategy.evaluate(ctx, NOW) is None


def test_float_filter_accepts_symbol_under_max(tmp_path):
    csv_path = write_float_list(tmp_path, [("GOGO", 8_000_000, datetime.now().date().isoformat())])
    ctx = make_ctx(BREAKOUT_BARS)
    strategy = GapAndGoStrategy(
        default_config(enable_float_filter=True, max_float_shares=10_000_000),
        float_provider=FloatProvider(csv_path),
    )
    assert strategy.evaluate(ctx, NOW) is not None


def test_float_filter_skips_symbol_missing_from_csv(tmp_path):
    csv_path = write_float_list(tmp_path, [("OTHER", 30_000_000, "2026-01-01")])
    ctx = make_ctx(BREAKOUT_BARS)
    strategy = GapAndGoStrategy(
        default_config(enable_float_filter=True, max_float_shares=10_000_000),
        float_provider=FloatProvider(csv_path),
    )
    assert strategy.evaluate(ctx, NOW) is not None


def test_float_filter_disabled_ignores_large_float(tmp_path):
    csv_path = write_float_list(tmp_path, [("GOGO", 30_000_000, datetime.now().date().isoformat())])
    ctx = make_ctx(BREAKOUT_BARS)
    strategy = GapAndGoStrategy(
        default_config(enable_float_filter=False, max_float_shares=10_000_000),
        float_provider=FloatProvider(csv_path),
    )
    assert strategy.evaluate(ctx, NOW) is not None


def test_reset_daily_allows_retrigger():
    ctx = make_ctx(
        [
            (5.5, 5.7, 5.4, 5.6, 1000),
            (5.6, 5.8, 5.5, 5.7, 1000),
            (5.7, 5.9, 5.6, 5.75, 1000),
            (5.75, 5.85, 5.7, 5.8, 1000),
            (5.8, 6.5, 5.8, 6.5, 1000),
        ]
    )
    strategy = GapAndGoStrategy(default_config())
    assert strategy.evaluate(ctx, NOW) is not None
    strategy.reset_daily()
    assert strategy.evaluate(ctx, NOW) is not None
