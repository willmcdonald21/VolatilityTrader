from __future__ import annotations

from datetime import datetime

from tests.unit.fixtures import make_bars
from warrior_bot.config import GapAndGoConfig
from warrior_bot.scanner.float_provider import FloatProvider
from warrior_bot.strategies import gap_and_go as gap_and_go_module
from warrior_bot.strategies.base_strategy import SymbolContext
from warrior_bot.strategies.gap_and_go import GapAndGoStrategy
from warrior_bot.strategies.pullback_validity import PullbackValidity
from warrior_bot.utils.time_utils import EASTERN

NOW = datetime(2026, 1, 5, 9, 35, tzinfo=EASTERN)  # ~5 min into RTH

# Reworked 2026-09-26: gap_and_go now enters on a spike -> pullback ->
# breakout, the same structure as bull_flag.py, instead of a straight
# opening-range breakout -- see gap_and_go.py's docstring. PASSING_BARS
# mirrors bull_flag's own PASSING_BARS price shape exactly (baseline, spike,
# 3-bar consolidation, breakout), just closing the breakout bar a little
# closer to the pullback high: gap_and_go's max_extension_pct default (2.0)
# is tighter than bull_flag's (3.0), so bull_flag's own 12.05 close (2.55%
# past its 11.75 flag high) would be rejected here.
PASSING_BARS = [
    (10.0, 10.0, 9.9, 10.0, 1000),      # baseline
    (10.0, 12.0, 10.0, 11.8, 3000),     # spike -- big move, heavy volume
    (11.8, 11.75, 11.6, 11.65, 300),    # consolidation 1 -- light volume
    (11.65, 11.7, 11.55, 11.6, 300),    # consolidation 2
    (11.6, 11.65, 11.5, 11.55, 300),    # consolidation 3
    (11.55, 11.95, 11.55, 11.95, 1000),  # breakout -- ~1.7% past the 11.75 pullback high, under the 2% extension cap
]


def make_ctx(bar_specs, prior_close=10.5, avg_daily_volume=10_000, symbol="GOGO"):
    # prior_close=10.5 pairs with PASSING_BARS' 11.95 breakout close for a
    # ~13.8% gap -- comfortably inside the default 10-20% gap band.
    ctx = SymbolContext(symbol=symbol)
    ctx.bars = make_bars(bar_specs)
    ctx.prior_close = prior_close
    ctx.avg_daily_volume = avg_daily_volume
    return ctx


def default_config(**overrides) -> GapAndGoConfig:
    return GapAndGoConfig(**overrides)


def test_breakout_triggers_signal():
    ctx = make_ctx(PASSING_BARS)
    strategy = GapAndGoStrategy(default_config())
    signal = strategy.evaluate(ctx, NOW)
    assert signal is not None
    assert signal.symbol == "GOGO"
    assert signal.strategy == "gap_and_go"
    assert signal.entry_price == 11.95
    assert signal.stop_price < signal.entry_price
    assert signal.target_price > signal.entry_price
    # prior bar (the last consolidation bar) closes at 11.55, still under the
    # $12 level -- the breakout bar's 11.95 close doesn't cross it. See
    # test_round_number_breakout_true_when_level_crossed for the True case.
    assert signal.context["round_number_breakout"] is False


def test_round_number_breakout_true_when_level_crossed():
    bars = PASSING_BARS[:-1] + [(11.55, 12.05, 11.55, 12.05, 1000)]
    ctx = make_ctx(bars)
    # 12.05 is ~2.55% past the 11.75 pullback high -- over the 2% default
    # cap, so the extension gate is loosened to isolate round_number_breakout.
    strategy = GapAndGoStrategy(default_config(max_extension_atr_multiple=100.0, max_extension_pct=100.0))
    signal = strategy.evaluate(ctx, NOW)
    assert signal is not None
    assert signal.context["round_number_breakout"] is True


def test_no_signal_without_breakout():
    bars = PASSING_BARS[:-1] + [(11.55, 11.7, 11.5, 11.6, 1000)]  # fails to clear the 11.75 pullback high
    ctx = make_ctx(bars)
    strategy = GapAndGoStrategy(default_config())
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
    ctx = make_ctx(bars, prior_close=9.5)  # gap ~16.3%, within band
    strategy = GapAndGoStrategy(default_config())
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
    ctx = make_ctx(bars, prior_close=9.8)  # gap ~14.3%, within band
    strategy = GapAndGoStrategy(default_config())
    assert strategy.evaluate(ctx, NOW) is None


SINGLE_BAR_PULLBACK = [
    (10.0, 10.0, 9.9, 10.0, 1000),      # baseline
    (10.0, 12.0, 10.0, 11.8, 3000),     # spike
    (11.8, 11.75, 11.5, 11.6, 300),     # single-bar pullback -- light volume
    (11.6, 11.95, 11.6, 11.95, 1000),   # breakout -- ~1.7% past the 11.75 pullback high
]


def test_single_bar_pullback_allowed_by_default():
    # "1 or more red candles" per source material -- a single-bar micro
    # pullback is the ideal case, not something the default should reject.
    ctx = make_ctx(SINGLE_BAR_PULLBACK)
    strategy = GapAndGoStrategy(default_config())
    assert strategy.evaluate(ctx, NOW) is not None


def test_pullback_rejected_by_pullback_quality(monkeypatch):
    # Confirms the wiring, not validate_pullback's own internal rules (see
    # test_pullback_validity.py for those) -- gap_and_go's pullback is
    # structurally the same shape bull_flag/abcd already validate.
    monkeypatch.setattr(
        gap_and_go_module,
        "validate_pullback",
        lambda **kwargs: PullbackValidity(False, "topping tail in pullback"),
    )
    ctx = make_ctx(PASSING_BARS)
    strategy = GapAndGoStrategy(default_config())
    assert strategy.evaluate(ctx, NOW) is None


def test_no_signal_when_gap_too_small():
    ctx = make_ctx(PASSING_BARS, prior_close=11.5)  # gap ~3.9%, under the 10% floor
    strategy = GapAndGoStrategy(default_config())
    assert strategy.evaluate(ctx, NOW) is None


def test_no_signal_when_gap_too_large():
    ctx = make_ctx(PASSING_BARS, prior_close=9.0)  # gap ~32.8%, over the 20% ceiling
    strategy = GapAndGoStrategy(default_config())
    assert strategy.evaluate(ctx, NOW) is None


def test_signal_when_max_gap_pct_disabled():
    ctx = make_ctx(PASSING_BARS, prior_close=9.0)
    strategy = GapAndGoStrategy(default_config(max_gap_pct=None))
    assert strategy.evaluate(ctx, NOW) is not None


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


def test_no_signal_when_breakout_candle_strength_below_default_threshold():
    # Closes above the pullback high (11.75) but red/weak-bodied.
    bars = PASSING_BARS[:-1] + [(12.1, 12.3, 11.8, 11.9, 1000)]
    ctx = make_ctx(bars)
    strategy = GapAndGoStrategy(default_config())
    assert strategy.evaluate(ctx, NOW) is None


def test_signal_when_breakout_candle_strength_gate_disabled():
    bars = PASSING_BARS[:-1] + [(12.1, 12.3, 11.8, 11.9, 1000)]
    ctx = make_ctx(bars)
    strategy = GapAndGoStrategy(default_config(min_breakout_candle_strength=-1.0))
    assert strategy.evaluate(ctx, NOW) is not None


def test_no_signal_when_relative_volume_too_low():
    ctx = make_ctx(PASSING_BARS, avg_daily_volume=10_000_000)  # tiny volume so far vs. a huge average
    strategy = GapAndGoStrategy(default_config())
    assert strategy.evaluate(ctx, NOW) is None


def test_no_signal_when_relative_volume_too_high():
    ctx = make_ctx(PASSING_BARS, avg_daily_volume=10)  # tiny average -> relative volume far above the 200x ceiling
    strategy = GapAndGoStrategy(default_config())
    assert strategy.evaluate(ctx, NOW) is None


def test_signal_when_max_rel_volume_disabled():
    ctx = make_ctx(PASSING_BARS, avg_daily_volume=10)
    strategy = GapAndGoStrategy(default_config(max_rel_volume=None))
    assert strategy.evaluate(ctx, NOW) is not None


def test_does_not_retrigger_same_symbol_same_day():
    ctx = make_ctx(PASSING_BARS)
    strategy = GapAndGoStrategy(default_config())
    first = strategy.evaluate(ctx, NOW)
    assert first is not None
    second = strategy.evaluate(ctx, NOW)
    assert second is None


def write_float_list(tmp_path, rows):
    path = tmp_path / "float_list.csv"
    lines = ["symbol,float_shares,updated_at"]
    for symbol, float_shares, updated_at in rows:
        lines.append(f"{symbol},{float_shares},{updated_at}")
    path.write_text("\n".join(lines) + "\n")
    return path


def test_float_filter_rejects_symbol_over_max(tmp_path):
    csv_path = write_float_list(tmp_path, [("GOGO", 30_000_000, datetime.now().date().isoformat())])
    ctx = make_ctx(PASSING_BARS)
    strategy = GapAndGoStrategy(
        default_config(enable_float_filter=True, max_float_shares=10_000_000),
        float_provider=FloatProvider(csv_path),
    )
    assert strategy.evaluate(ctx, NOW) is None


def test_float_filter_accepts_symbol_under_max(tmp_path):
    csv_path = write_float_list(tmp_path, [("GOGO", 8_000_000, datetime.now().date().isoformat())])
    ctx = make_ctx(PASSING_BARS)
    strategy = GapAndGoStrategy(
        default_config(enable_float_filter=True, max_float_shares=10_000_000),
        float_provider=FloatProvider(csv_path),
    )
    assert strategy.evaluate(ctx, NOW) is not None


def test_float_filter_skips_symbol_missing_from_csv(tmp_path):
    csv_path = write_float_list(tmp_path, [("OTHER", 30_000_000, "2026-01-01")])
    ctx = make_ctx(PASSING_BARS)
    strategy = GapAndGoStrategy(
        default_config(enable_float_filter=True, max_float_shares=10_000_000),
        float_provider=FloatProvider(csv_path),
    )
    assert strategy.evaluate(ctx, NOW) is not None


def test_float_filter_disabled_ignores_large_float(tmp_path):
    csv_path = write_float_list(tmp_path, [("GOGO", 30_000_000, datetime.now().date().isoformat())])
    ctx = make_ctx(PASSING_BARS)
    strategy = GapAndGoStrategy(
        default_config(enable_float_filter=False, max_float_shares=10_000_000),
        float_provider=FloatProvider(csv_path),
    )
    assert strategy.evaluate(ctx, NOW) is not None


def test_float_rotation_rejects_symbol_under_threshold(tmp_path):
    csv_path = write_float_list(tmp_path, [("GOGO", 1_000_000, datetime.now().date().isoformat())])
    ctx = make_ctx(PASSING_BARS)  # cumulative volume across PASSING_BARS = 5900
    strategy = GapAndGoStrategy(
        default_config(min_float_rotation=10.0),  # would need cum_volume/float >= 10 -> 10,000,000 volume
        float_provider=FloatProvider(csv_path),
    )
    assert strategy.evaluate(ctx, NOW) is None


def test_float_rotation_accepts_symbol_over_threshold(tmp_path):
    csv_path = write_float_list(tmp_path, [("GOGO", 1_000, datetime.now().date().isoformat())])
    ctx = make_ctx(PASSING_BARS)  # cumulative volume = 5900 -> rotation = 5.9
    strategy = GapAndGoStrategy(
        default_config(min_float_rotation=2.0),
        float_provider=FloatProvider(csv_path),
    )
    assert strategy.evaluate(ctx, NOW) is not None


def test_float_rotation_disabled_by_default_ignores_low_rotation(tmp_path):
    csv_path = write_float_list(tmp_path, [("GOGO", 1_000_000_000, datetime.now().date().isoformat())])
    ctx = make_ctx(PASSING_BARS)
    strategy = GapAndGoStrategy(
        default_config(enable_float_filter=False),  # min_float_rotation=0.0 by default -- isolate rotation from the existing float-size filter
        float_provider=FloatProvider(csv_path),
    )
    assert strategy.evaluate(ctx, NOW) is not None


def test_float_rotation_skips_symbol_missing_from_csv(tmp_path):
    csv_path = write_float_list(tmp_path, [("OTHER", 1_000, "2026-01-01")])
    ctx = make_ctx(PASSING_BARS)
    strategy = GapAndGoStrategy(
        default_config(min_float_rotation=1000.0),  # would reject any known float
        float_provider=FloatProvider(csv_path),
    )
    assert strategy.evaluate(ctx, NOW) is not None


def test_no_signal_when_breakout_bar_too_extended():
    # Mirrors GDC, 2026-09-22: a breakout bar that already ran far past the
    # pullback high within that single 1-minute bar, then reversed and hit
    # the stop within minutes -- an entry-quality problem the older
    # min_breakout_candle_strength check doesn't catch.
    bars = PASSING_BARS[:-1] + [(11.55, 14.0, 11.55, 14.0, 1000)]
    ctx = make_ctx(bars)
    # max_gap_pct loosened -- this bar's 14.0 close vs. prior_close 10.5 is a
    # ~33% gap that would otherwise get rejected before the extension gate
    # this test isolates ever runs.
    strategy = GapAndGoStrategy(default_config(max_gap_pct=100.0))
    assert strategy.evaluate(ctx, NOW) is None


def test_signal_when_extension_gate_loosened_enough():
    # Two independent gates now (see is_entry_too_extended) -- both must be
    # loosened for this bar to get through.
    bars = PASSING_BARS[:-1] + [(11.55, 14.0, 11.55, 14.0, 1000)]
    ctx = make_ctx(bars)
    strategy = GapAndGoStrategy(
        default_config(max_extension_atr_multiple=100.0, max_extension_pct=100.0, max_gap_pct=100.0)
    )
    assert strategy.evaluate(ctx, NOW) is not None


def test_no_signal_when_only_the_atr_gate_is_loosened():
    # Confirmed live, 2026-09-25: this was exactly the live bug -- the ATR
    # gate alone fired zero times ever (a stock's own trailing ATR inflates
    # as it spikes, loosening the multiple right when it should tighten).
    # max_extension_pct is the gate that actually has to hold here.
    bars = PASSING_BARS[:-1] + [(11.55, 14.0, 11.55, 14.0, 1000)]
    ctx = make_ctx(bars)
    strategy = GapAndGoStrategy(default_config(max_extension_atr_multiple=100.0, max_gap_pct=100.0))
    assert strategy.evaluate(ctx, NOW) is None


def test_signal_when_breakout_bar_close_to_the_level():
    # A controlled breakout -- closes just past the level, not blocked.
    ctx = make_ctx(PASSING_BARS)
    strategy = GapAndGoStrategy(default_config())
    assert strategy.evaluate(ctx, NOW) is not None


def test_reset_daily_allows_retrigger():
    ctx = make_ctx(PASSING_BARS)
    strategy = GapAndGoStrategy(default_config())
    assert strategy.evaluate(ctx, NOW) is not None
    strategy.reset_daily()
    assert strategy.evaluate(ctx, NOW) is not None
