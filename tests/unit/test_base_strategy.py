from __future__ import annotations

from datetime import datetime, timezone

from tests.unit.fixtures import make_bars
from warrior_bot.strategies.base_strategy import BaseStrategy, SymbolContext


class DummyConfig:
    enabled = True


class DummyStrategy(BaseStrategy):
    name = "dummy"

    def evaluate(self, ctx, now):
        return None


def make_strategy() -> DummyStrategy:
    return DummyStrategy(DummyConfig())


def test_ema_200_none_with_insufficient_bars():
    ctx = SymbolContext(symbol="TEST")
    ctx.bars = make_bars([(10.0, 10.0, 10.0, 10.0, 100)] * 60)  # matches the bot's real warmup window
    assert ctx.ema_200 is None


def test_ema_200_computed_once_enough_bars_available():
    ctx = SymbolContext(symbol="TEST")
    ctx.bars = make_bars([(10.0, 10.0, 10.0, 10.0, 100)] * 200)
    assert ctx.ema_200 == 10.0


def test_build_signal_attaches_catalyst_context_when_present():
    strategy = make_strategy()
    ctx = SymbolContext(symbol="TEST", catalyst_category="earnings", catalyst_headline="XYZ Reports Earnings")

    signal = strategy._build_signal(
        ctx, datetime.now(timezone.utc), entry_price=10.0, stop_price=9.0, target_r_multiple=2.0
    )

    assert signal.context["catalyst_category"] == "earnings"
    assert signal.context["catalyst_headline"] == "XYZ Reports Earnings"


def test_build_signal_omits_catalyst_context_when_absent():
    strategy = make_strategy()
    ctx = SymbolContext(symbol="TEST")

    signal = strategy._build_signal(
        ctx, datetime.now(timezone.utc), entry_price=10.0, stop_price=9.0, target_r_multiple=2.0
    )

    assert "catalyst_category" not in signal.context


def test_build_signal_preserves_caller_supplied_context_alongside_catalyst():
    strategy = make_strategy()
    ctx = SymbolContext(symbol="TEST", catalyst_category="fda", catalyst_headline="XYZ Gets FDA Approval")

    signal = strategy._build_signal(
        ctx,
        datetime.now(timezone.utc),
        entry_price=10.0,
        stop_price=9.0,
        target_r_multiple=2.0,
        context={"spike_high": 12.0},
    )

    assert signal.context["spike_high"] == 12.0
    assert signal.context["catalyst_category"] == "fda"


def test_build_signal_attaches_scanner_rank_when_present():
    strategy = make_strategy()
    ctx = SymbolContext(symbol="TEST", scanner_rank=2)

    signal = strategy._build_signal(
        ctx, datetime.now(timezone.utc), entry_price=10.0, stop_price=9.0, target_r_multiple=2.0
    )

    assert signal.context["scanner_rank"] == 2


def test_build_signal_omits_scanner_rank_when_absent():
    strategy = make_strategy()
    ctx = SymbolContext(symbol="TEST")

    signal = strategy._build_signal(
        ctx, datetime.now(timezone.utc), entry_price=10.0, stop_price=9.0, target_r_multiple=2.0
    )

    assert "scanner_rank" not in signal.context


def _make_ctx_with_macd_trend(rising: bool) -> SymbolContext:
    closes = [15.0] * 20 + [15.0 + (0.5 if rising else -0.5) * i for i in range(1, 15)]
    ctx = SymbolContext(symbol="TEST")
    ctx.bars = make_bars([(c, c, c, c, 1000) for c in closes])
    return ctx


def test_check_engaged_true_when_insufficient_data_for_macd():
    strategy = make_strategy()
    ctx = SymbolContext(symbol="TEST")  # no bars -- macd(9, 20) is None
    assert strategy._check_engaged(ctx) is True


def test_check_engaged_true_when_macd_bullish():
    strategy = make_strategy()
    ctx = _make_ctx_with_macd_trend(rising=True)
    assert strategy._check_engaged(ctx) is True


def test_check_engaged_false_after_bearish_macd_crossover():
    strategy = make_strategy()
    ctx = _make_ctx_with_macd_trend(rising=False)
    assert strategy._check_engaged(ctx) is False


def test_check_engaged_re_engages_once_macd_turns_bullish_again():
    strategy = make_strategy()
    bearish_ctx = _make_ctx_with_macd_trend(rising=False)
    assert strategy._check_engaged(bearish_ctx) is False

    # same symbol, later bar, MACD now bullish -- should be reconsidered,
    # not permanently excluded from the bearish check above
    bullish_ctx = _make_ctx_with_macd_trend(rising=True)
    bullish_ctx.symbol = "TEST"
    assert strategy._check_engaged(bullish_ctx) is True
