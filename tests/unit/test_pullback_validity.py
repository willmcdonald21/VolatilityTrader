from __future__ import annotations

from tests.unit.fixtures import make_bars
from warrior_bot.config import PullbackQualityConfig
from warrior_bot.strategies.base_strategy import SymbolContext
from warrior_bot.strategies.pullback_validity import validate_pullback


def make_ctx(bar_specs, symbol="TEST"):
    ctx = SymbolContext(symbol=symbol)
    ctx.bars = make_bars(bar_specs)
    return ctx


def test_passes_when_no_data_available_for_soft_gates():
    # Only 2 bars total -- not enough for a meaningful ema_9 or macd (both
    # gracefully skip when unavailable); vwap needs only one bar and is
    # deliberately kept clear here so this isolates the ema_9/macd
    # "insufficient data never blocks" behavior specifically.
    ctx = make_ctx([(10.0, 10.5, 10.0, 10.4, 1000), (10.4, 10.45, 10.35, 10.4, 200)])
    up_move = make_bars([(10.0, 10.5, 10.0, 10.4, 1000)])
    pullback = make_bars([(10.4, 10.45, 10.35, 10.4, 200)])
    result = validate_pullback(pullback, up_move, ctx)
    assert result.valid


def test_passes_when_pullback_or_up_move_bars_empty():
    ctx = make_ctx([(10.0, 10.5, 10.0, 10.4, 1000)])
    assert validate_pullback([], [], ctx).valid


def test_rejects_when_volume_declines_on_the_advance():
    ctx = make_ctx([(10.0, 10.5, 10.0, 10.4, 1000), (10.4, 10.45, 10.35, 10.4, 200)])
    up_move = make_bars([(10.0, 10.2, 10.0, 10.15, 2000), (10.15, 10.4, 10.1, 10.35, 1000)])  # declining volume
    pullback = make_bars([(10.35, 10.4, 10.3, 10.35, 50)])

    result = validate_pullback(pullback, up_move, ctx)

    assert not result.valid
    assert result.reason == "volume declining on the preceding advance"


def test_rising_volume_gate_disabled_via_config():
    ctx = make_ctx([(10.0, 10.5, 10.0, 10.4, 1000), (10.4, 10.45, 10.35, 10.4, 200)])
    up_move = make_bars([(10.0, 10.2, 10.0, 10.15, 2000), (10.15, 10.4, 10.1, 10.35, 1000)])
    pullback = make_bars([(10.35, 10.4, 10.33, 10.35, 50)])  # low clears ctx's vwap (~10.317)
    config = PullbackQualityConfig(require_rising_volume_on_advance=False)

    result = validate_pullback(pullback, up_move, ctx, config=config)

    assert result.valid


def test_rejects_when_pullback_volume_not_lighter_than_up_move():
    ctx = make_ctx([(10.0, 10.5, 10.0, 10.4, 500), (10.4, 10.45, 10.3, 10.35, 800)])
    up_move = make_bars([(10.0, 10.5, 10.0, 10.4, 500)])
    pullback = make_bars([(10.4, 10.45, 10.3, 10.35, 800)])  # heavier than the up-move

    result = validate_pullback(pullback, up_move, ctx)

    assert not result.valid
    assert "volume" in result.reason


def test_rejects_when_pullback_breaks_vwap():
    # heavy volume at 10.0 anchors vwap near there; a light-volume pullback
    # that dips well below it should be rejected
    ctx = make_ctx([(10.0, 10.0, 10.0, 10.0, 5000), (10.0, 10.9, 10.0, 10.8, 100)])
    up_move = make_bars([(10.0, 10.9, 10.0, 10.8, 100)])
    pullback = make_bars([(10.8, 10.85, 9.5, 10.0, 10)])  # low of 9.5, well below vwap

    result = validate_pullback(pullback, up_move, ctx)

    assert not result.valid
    assert "VWAP" in result.reason


def test_rejects_when_pullback_breaks_9_ema():
    # 9 bars: one low-price/heavy-volume bar keeps vwap low, the rest flat
    # at a higher price keep ema_9 high -- isolates the ema_9 gate from vwap
    ctx = make_ctx(
        [
            (9.0, 9.0, 9.0, 9.0, 5000),
            (10.0, 10.0, 10.0, 10.0, 100),
            (10.0, 10.0, 10.0, 10.0, 100),
            (10.0, 10.0, 10.0, 10.0, 100),
            (10.0, 10.0, 10.0, 10.0, 100),
            (10.0, 10.0, 10.0, 10.0, 100),
            (10.0, 10.0, 10.0, 10.0, 100),
            (10.0, 10.0, 10.0, 10.0, 100),
            (10.0, 10.0, 10.0, 10.0, 100),
        ]
    )
    assert ctx.vwap < 9.2  # sanity check: vwap sits well below the pullback low used
    assert ctx.ema_9 > 9.7  # sanity check: ema_9 sits above the pullback low used

    up_move = make_bars([(9.4, 9.6, 9.4, 9.5, 1000)])
    pullback = make_bars([(9.5, 9.6, 9.5, 9.55, 10)])  # low of 9.5: above vwap, below ema_9

    result = validate_pullback(pullback, up_move, ctx)

    assert not result.valid
    assert "EMA" in result.reason


def test_passes_when_pullback_wick_dips_below_9_ema_but_closes_above_it():
    # Same 9-bar ctx shape as test_rejects_when_pullback_breaks_9_ema, but
    # the pullback bar's LOW wicks below the 9 EMA while its CLOSE
    # reclaims above it -- source material's explicit "a brief
    # single-candle wick... that immediately reclaims" tolerance. A
    # single-bar up_move makes has_rising_volume_on_advance() a
    # no-op (insufficient data), isolating this to the 9 EMA gate.
    ctx = make_ctx(
        [
            (9.0, 9.0, 9.0, 9.0, 5000),
            (10.0, 10.0, 10.0, 10.0, 100),
            (10.0, 10.0, 10.0, 10.0, 100),
            (10.0, 10.0, 10.0, 10.0, 100),
            (10.0, 10.0, 10.0, 10.0, 100),
            (10.0, 10.0, 10.0, 10.0, 100),
            (10.0, 10.0, 10.0, 10.0, 100),
            (10.0, 10.0, 10.0, 10.0, 100),
            (10.0, 10.0, 10.0, 10.0, 100),
        ]
    )
    assert ctx.vwap < 9.2
    assert 9.7 < ctx.ema_9 < 9.95

    up_move = make_bars([(9.4, 9.6, 9.4, 9.5, 1000)])
    pullback = make_bars([(9.75, 10.0, 9.5, 9.97, 10)])  # low 9.5 (below ema_9), close 9.97 (above it)

    result = validate_pullback(pullback, up_move, ctx)

    assert result.valid


def test_rejects_when_macd_not_bullish():
    # flat for 20 bars then a sharp sustained decline for 14 -- macd line
    # plunges negative faster than its own (lagging) signal line
    closes = [15.0] * 20 + [15.0 - 0.5 * i for i in range(1, 15)]
    ctx = make_ctx([(c, c, c, c, 1000) for c in closes])
    macd_result = ctx.macd(fast=9, slow=20)  # (9, 20) matches what validate_pullback actually checks
    assert macd_result is not None
    assert macd_result[0] <= macd_result[1]  # sanity check: bearish as constructed

    # ctx's own vwap (~13.5) and ema_9 (~9.9) are both well below this
    # pullback's low -- keeps this isolated to the MACD gate specifically
    up_move = make_bars([(15.0, 15.5, 15.0, 15.4, 1000)])
    pullback = make_bars([(15.4, 15.45, 14.0, 15.0, 50)])

    result = validate_pullback(pullback, up_move, ctx)

    assert not result.valid
    assert "MACD" in result.reason


def test_rejects_when_topping_tail_in_pullback():
    # 2-bar ctx: too little history for ema_9/1-min-macd/5-min-macd (all
    # gracefully skip), isolating the new topping-tail-in-pullback gate.
    # vwap ~= 10.317 here, so the pullback low (10.4) clears it.
    ctx = make_ctx([(10.0, 10.5, 10.0, 10.4, 1000), (10.4, 10.45, 10.35, 10.4, 200)])
    up_move = make_bars([(10.0, 10.5, 10.0, 10.4, 1000)])
    pullback = make_bars([(10.4, 11.0, 10.4, 10.45, 50)])  # long upper wick, tiny body

    result = validate_pullback(pullback, up_move, ctx)

    assert not result.valid
    assert result.reason == "topping tail in pullback"


def test_rejects_when_high_volume_red_bar_in_pullback():
    # 4 up-move bars, the last one a much bigger "spike" bar (volume 3000)
    # -- avg_recent_volume=825, threshold=1650 -- so the pullback bar's
    # volume (2000) clears the high-volume-red-bar threshold while still
    # being lighter than both the up-move's *total* volume (3300, the
    # aggregate gate) and the immediately preceding green candle's volume
    # alone (3000, the pairwise gate), isolating this gate from those two.
    up_move = make_bars(
        [
            (10.0, 10.1, 10.0, 10.1, 100),
            (10.1, 10.2, 10.1, 10.2, 100),
            (10.2, 10.3, 10.2, 10.3, 100),
            (10.3, 10.4, 10.3, 10.4, 3000),
        ]
    )
    ctx = make_ctx(
        [
            (10.0, 10.1, 10.0, 10.1, 100),
            (10.1, 10.2, 10.1, 10.2, 100),
            (10.2, 10.3, 10.2, 10.3, 100),
            (10.3, 10.4, 10.3, 10.4, 3000),
        ]
    )
    pullback = make_bars([(10.5, 10.55, 10.4, 10.45, 2000)])  # red, volume >= 2x avg_recent_volume(825)

    result = validate_pullback(pullback, up_move, ctx)

    assert not result.valid
    assert result.reason == "high-volume red bar in pullback"


def test_rejects_when_pullback_bar_exceeds_immediately_preceding_green_candle():
    # Two up-move bars (baseline 100, spike 200 -- sum 300) and two
    # pullback bars (210, 80 -- sum 290, *lighter* than the up-move total,
    # so the aggregate gate passes) but the first pullback bar (210) alone
    # exceeds the specific green spike candle right before it (200) --
    # isolates the new pairwise gate from the aggregate one.
    ctx = make_ctx([(10.0, 10.5, 10.0, 10.4, 1000), (10.4, 10.45, 10.35, 10.4, 200)])
    up_move = make_bars([(10.0, 10.2, 10.0, 10.15, 100), (10.15, 10.6, 10.15, 10.5, 200)])
    pullback = make_bars([(10.5, 10.55, 10.4, 10.45, 210), (10.45, 10.48, 10.4, 10.42, 80)])

    result = validate_pullback(pullback, up_move, ctx)

    assert not result.valid
    assert result.reason == "pullback bar volume not lighter than the immediately preceding green candle"


def test_pairwise_volume_gate_disabled_via_config():
    ctx = make_ctx([(10.0, 10.5, 10.0, 10.4, 1000), (10.4, 10.45, 10.35, 10.4, 200)])
    up_move = make_bars([(10.0, 10.2, 10.0, 10.15, 100), (10.15, 10.6, 10.15, 10.5, 200)])
    pullback = make_bars([(10.5, 10.55, 10.4, 10.45, 210), (10.45, 10.48, 10.4, 10.42, 80)])
    config = PullbackQualityConfig(require_pullback_lighter_than_prior_green_bar=False)

    result = validate_pullback(pullback, up_move, ctx, config=config)

    assert result.valid


def test_dump_checklist_gates_disabled_via_config():
    # Same topping-tail pullback bar as above, but every new gate disabled
    # -- should pass, proving the checks are genuinely opt-out, not just
    # individually threshold-tunable.
    ctx = make_ctx([(10.0, 10.5, 10.0, 10.4, 1000), (10.4, 10.45, 10.35, 10.4, 200)])
    up_move = make_bars([(10.0, 10.5, 10.0, 10.4, 1000)])
    pullback = make_bars([(10.4, 11.0, 10.4, 10.45, 50)])
    config = PullbackQualityConfig(
        reject_topping_tail=False,
        reject_high_volume_red_bar=False,
        require_5m_macd_confirmation=False,
        reject_5m_topping_tail=False,
        require_pullback_lighter_than_prior_green_bar=False,
        require_rising_volume_on_advance=False,
    )

    result = validate_pullback(pullback, up_move, ctx, config=config)

    assert result.valid


def test_valid_pullback_with_all_gates_available_and_passing():
    closes = [15.0] * 20 + [15.0 + 0.5 * i for i in range(1, 15)]  # sustained rise -> bullish MACD
    ctx = make_ctx([(c, c, c, c, 1000) for c in closes])
    macd_result = ctx.macd(fast=9, slow=20)
    assert macd_result is not None
    assert macd_result[0] > macd_result[1]  # sanity check: bullish as constructed

    up_move = make_bars([(20.0, 21.0, 20.0, 20.9, 1000)])
    pullback = make_bars([(20.9, 20.95, 20.8, 20.85, 100)])  # light volume, well above ctx's price levels

    result = validate_pullback(pullback, up_move, ctx)

    assert result.valid
    assert result.reason is None


def test_rejects_on_5_minute_macd_veto_despite_bullish_1_minute_macd():
    # Three phases: a long gentle decline (100 bars), an accelerating
    # steeper decline into the reversal (30 bars), then a bounce (9 bars).
    # The accelerating phase keeps the slower-reacting 5-minute MACD
    # (9/20-*bucket* EMAs) still clearly bearish at the moment of the
    # bounce, while the fast-reacting 1-minute MACD (9/20-*bar* EMAs) picks
    # up the bounce quickly enough to read bullish -- exactly the "clean
    # fast timeframe, deteriorating slow timeframe" divergence the
    # multi-timeframe veto exists to catch. Volume is deliberately tiny on
    # the long history and real only on the last few bounce bars, so
    # ctx.vwap tracks the recent price level instead of the historical
    # decline (same isolation technique the existing vwap/ema tests use) --
    # otherwise the earlier VWAP gate would reject first and this
    # wouldn't isolate the new 5-minute check. (Parameters found by
    # search, not hand-derived -- MACD crossover timing on a resampled
    # series isn't tractable to compute by hand; see the sanity-check
    # asserts below.)
    closes = [500.0 - 0.1 * i for i in range(100)]
    for i in range(1, 31):
        closes.append(closes[-1] - 0.3 * i)
    for _ in range(9):
        closes.append(closes[-1] + 2.0)
    volumes = [0.01] * (len(closes) - 6) + [1000.0] * 6
    ctx = make_ctx(list(zip(closes, closes, closes, closes, volumes)))

    one_min_macd = ctx.macd(fast=9, slow=20)
    assert one_min_macd is not None
    assert one_min_macd[0] > one_min_macd[1]  # sanity check: bullish as constructed

    from warrior_bot.strategies.indicators import macd, resample_bars

    five_min_macd = macd(resample_bars(ctx.bars, bucket_minutes=5), fast=9, slow=20)
    assert five_min_macd is not None
    assert five_min_macd[0] <= five_min_macd[1]  # sanity check: still bearish as constructed

    up_move = make_bars([(closes[-2], closes[-2] + 0.1, closes[-2], closes[-1], 1000)])
    pullback = make_bars([(closes[-1], closes[-1] + 0.05, closes[-1] - 0.05, closes[-1], 50)])
    assert pullback[0].low >= ctx.vwap  # sanity check: isolates the 5-min gate from the VWAP gate
    assert pullback[0].low >= ctx.ema_9  # sanity check: isolates the 5-min gate from the 9 EMA gate

    result = validate_pullback(pullback, up_move, ctx)

    assert not result.valid
    assert result.reason == "5-minute MACD not bullish (multi-timeframe veto)"
