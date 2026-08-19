from __future__ import annotations

from warrior_bot.scanner.regime import count_extreme_gainers
from warrior_bot.strategies.base_strategy import SymbolContext
from warrior_bot.strategies.indicators import Bar
from datetime import datetime, timezone


def make_ctx_with_gap(prior_close: float, last_price: float) -> SymbolContext:
    ctx = SymbolContext(symbol="TEST", prior_close=prior_close)
    ctx.bars = [Bar(time=datetime.now(timezone.utc), open=last_price, high=last_price, low=last_price, close=last_price, volume=100)]
    return ctx


def test_counts_symbols_at_or_above_threshold():
    contexts = [
        make_ctx_with_gap(prior_close=5.0, last_price=11.0),  # +120%
        make_ctx_with_gap(prior_close=5.0, last_price=10.0),  # +100%, boundary -- included
        make_ctx_with_gap(prior_close=5.0, last_price=7.0),  # +40% -- not extreme
    ]
    assert count_extreme_gainers(contexts) == 2


def test_ignores_contexts_with_no_gap_data():
    contexts = [SymbolContext(symbol="NOPRIOR")]  # no prior_close -> gap_pct is None
    assert count_extreme_gainers(contexts) == 0


def test_empty_contexts_returns_zero():
    assert count_extreme_gainers([]) == 0


def test_custom_threshold():
    contexts = [make_ctx_with_gap(prior_close=5.0, last_price=7.5)]  # +50%
    assert count_extreme_gainers(contexts, threshold_pct=50.0) == 1
    assert count_extreme_gainers(contexts, threshold_pct=100.0) == 0
