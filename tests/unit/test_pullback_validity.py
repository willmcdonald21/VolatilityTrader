from __future__ import annotations

from tests.unit.fixtures import make_bars
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
