from __future__ import annotations

from datetime import datetime, timezone

from warrior_bot.strategies.base_strategy import BaseStrategy, SymbolContext


class DummyConfig:
    enabled = True


class DummyStrategy(BaseStrategy):
    name = "dummy"

    def evaluate(self, ctx, now):
        return None


def make_strategy() -> DummyStrategy:
    return DummyStrategy(DummyConfig())


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
