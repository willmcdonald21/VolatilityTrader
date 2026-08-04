from __future__ import annotations

from datetime import datetime, timezone

from warrior_bot.config import RiskConfig
from warrior_bot.risk.account_state import AccountSnapshot
from warrior_bot.risk.risk_manager import RiskManager
from warrior_bot.signals.signal import Signal


class FakeAccountState:
    def __init__(self, snapshot: AccountSnapshot):
        self._snapshot = snapshot

    def snapshot(self) -> AccountSnapshot:
        return self._snapshot


def make_signal(entry=10.0, stop=9.0, target=12.0) -> Signal:
    return Signal(
        symbol="TEST",
        strategy="gap_and_go",
        side="BUY",
        entry_price=entry,
        stop_price=stop,
        target_price=target,
        ts=datetime.now(timezone.utc),
    )


def make_risk_manager(tmp_path, snapshot, **risk_overrides) -> RiskManager:
    config = RiskConfig(
        risk_per_trade_pct=risk_overrides.get("risk_per_trade_pct", 0.01),
        daily_loss_limit_pct=risk_overrides.get("daily_loss_limit_pct", 0.02),
        max_concurrent_positions=risk_overrides.get("max_concurrent_positions", 3),
        max_position_notional_usd=risk_overrides.get("max_position_notional_usd", 5000),
        max_shares_per_trade=risk_overrides.get("max_shares_per_trade", 2000),
    )
    account_state = FakeAccountState(snapshot)
    return RiskManager(config, account_state, kill_switch_path=tmp_path / "KILL_SWITCH")


def default_snapshot(**overrides) -> AccountSnapshot:
    return AccountSnapshot(
        net_liquidation=overrides.get("net_liquidation", 100_000),
        available_funds=overrides.get("available_funds", 100_000),
        buying_power=overrides.get("buying_power", 200_000),
        open_positions_count=overrides.get("open_positions_count", 0),
        daily_realized_pnl=overrides.get("daily_realized_pnl", 0.0),
    )


def test_accepts_signal_and_sizes_by_risk_pct(tmp_path):
    snapshot = default_snapshot(net_liquidation=100_000)
    rm = make_risk_manager(tmp_path, snapshot, risk_per_trade_pct=0.01)
    signal = make_signal(entry=10.0, stop=9.0)  # risk_per_share=1.0

    decision = rm.evaluate(signal)

    assert decision.accepted
    # dollar_risk_budget = 100_000 * 0.01 = 1000 -> 1000 shares, capped by notional/shares caps below
    assert decision.sized_qty == 500  # capped by max_position_notional_usd(5000)/entry(10) = 500


def test_rejects_when_kill_switch_flag_file_present(tmp_path):
    snapshot = default_snapshot()
    rm = make_risk_manager(tmp_path, snapshot)
    (tmp_path / "KILL_SWITCH").write_text("halt")

    decision = rm.evaluate(make_signal())

    assert not decision.accepted
    assert "kill switch" in decision.reason


def test_rejects_when_manual_kill_switch_activated(tmp_path):
    snapshot = default_snapshot()
    rm = make_risk_manager(tmp_path, snapshot)
    rm.activate_kill_switch()

    decision = rm.evaluate(make_signal())

    assert not decision.accepted
    assert "kill switch" in decision.reason


def test_rejects_when_daily_loss_limit_breached(tmp_path):
    snapshot = default_snapshot(net_liquidation=100_000, daily_realized_pnl=-2500)
    rm = make_risk_manager(tmp_path, snapshot, daily_loss_limit_pct=0.02)  # limit = 2000

    decision = rm.evaluate(make_signal())

    assert not decision.accepted
    assert "daily loss limit" in decision.reason


def test_rejects_when_max_concurrent_positions_reached(tmp_path):
    snapshot = default_snapshot(open_positions_count=3)
    rm = make_risk_manager(tmp_path, snapshot, max_concurrent_positions=3)

    decision = rm.evaluate(make_signal())

    assert not decision.accepted
    assert "max concurrent positions" in decision.reason


def test_rejects_when_sized_qty_rounds_to_zero(tmp_path):
    snapshot = default_snapshot(net_liquidation=100_000, buying_power=200_000)
    rm = make_risk_manager(tmp_path, snapshot, risk_per_trade_pct=0.00001)
    # dollar_risk_budget=1.0, risk_per_share=9.0 -> raw_shares floors to 0
    signal = make_signal(entry=10.0, stop=1.0)

    decision = rm.evaluate(signal)

    assert not decision.accepted
    assert "rounds to zero" in decision.reason


def test_sizing_capped_by_buying_power(tmp_path):
    snapshot = default_snapshot(net_liquidation=1_000_000, buying_power=100)
    rm = make_risk_manager(tmp_path, snapshot, risk_per_trade_pct=0.05, max_position_notional_usd=1_000_000, max_shares_per_trade=100_000)
    signal = make_signal(entry=10.0, stop=9.0)

    decision = rm.evaluate(signal)

    assert decision.accepted
    assert decision.sized_qty == 10  # buying_power(100) / entry(10)
