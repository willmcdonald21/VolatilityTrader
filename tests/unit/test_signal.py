from __future__ import annotations

from datetime import datetime, timezone

from warrior_bot.signals.signal import Signal


def make_dirty_signal() -> Signal:
    return Signal(
        symbol="TEST",
        strategy="gap_and_go",
        side="BUY",
        entry_price=13.474999,
        stop_price=13.21683375,
        target_price=13.4751,
        ts=datetime.now(timezone.utc),
    )


def test_signal_rounds_entry_price_on_construction():
    signal = make_dirty_signal()
    assert signal.entry_price == round(signal.entry_price, 2)


def test_signal_rounds_stop_price_on_construction():
    signal = make_dirty_signal()
    assert signal.stop_price == 13.22


def test_signal_rounds_target_price_on_construction():
    signal = make_dirty_signal()
    assert signal.target_price == 13.48


def test_risk_per_share_reflects_rounded_prices():
    signal = Signal(
        symbol="TEST",
        strategy="gap_and_go",
        side="BUY",
        entry_price=10.0,
        stop_price=9.21683375,
        target_price=12.0,
        ts=datetime.now(timezone.utc),
    )
    # stop_price is rounded to 9.22 at construction -- risk_per_share must
    # be computed from that rounded value, not the raw input, since 9.22
    # is what actually gets submitted to IBKR as the protective stop.
    assert signal.stop_price == 9.22
    assert signal.risk_per_share == 10.0 - 9.22
