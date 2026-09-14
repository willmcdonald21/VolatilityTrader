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


class FakePositionManager:
    """Stands in for PositionManager.open_lot_count -- the number of
    already-open lots RiskManager should treat this symbol as holding."""

    def __init__(self, open_lots: int = 0):
        self._open_lots = open_lots

    def open_lot_count(self, symbol: str) -> int:
        return self._open_lots


def make_signal(entry=10.0, stop=9.0, target=12.0, context=None) -> Signal:
    return Signal(
        symbol="TEST",
        strategy="gap_and_go",
        side="BUY",
        entry_price=entry,
        stop_price=stop,
        target_price=target,
        ts=datetime.now(timezone.utc),
        context=context or {},
    )


def make_risk_manager(tmp_path, snapshot, open_lots: int = 0, **risk_overrides) -> RiskManager:
    config = RiskConfig(
        daily_loss_limit_pct=risk_overrides.get("daily_loss_limit_pct", 0.02),
        max_concurrent_positions=risk_overrides.get("max_concurrent_positions", 3),
        reserved_top_tier_slots=risk_overrides.get("reserved_top_tier_slots", 0),
        reserved_top_tier_max_rank=risk_overrides.get("reserved_top_tier_max_rank", 3),
        max_position_pct_of_buying_power=risk_overrides.get("max_position_pct_of_buying_power", 0.25),
        daily_profit_goal_usd=risk_overrides.get("daily_profit_goal_usd"),
        cushion_profit_fraction=risk_overrides.get("cushion_profit_fraction", 0.25),
        cushion_size_fraction=risk_overrides.get("cushion_size_fraction", 0.25),
        first_entry_pct_of_funds=risk_overrides.get("first_entry_pct_of_funds", 0.10),
        addon_pct_of_funds=risk_overrides.get("addon_pct_of_funds", 0.05),
    )
    account_state = FakeAccountState(snapshot)
    position_manager = FakePositionManager(open_lots)
    return RiskManager(config, account_state, position_manager, kill_switch_path=tmp_path / "KILL_SWITCH")


def default_snapshot(**overrides) -> AccountSnapshot:
    return AccountSnapshot(
        net_liquidation=overrides.get("net_liquidation", 100_000),
        available_funds=overrides.get("available_funds", 100_000),
        # Large enough by default that max_position_pct_of_buying_power
        # never binds unless a test deliberately sets it low to isolate
        # that cap.
        buying_power=overrides.get("buying_power", 1_000_000),
        open_positions_count=overrides.get("open_positions_count", 0),
        daily_realized_pnl=overrides.get("daily_realized_pnl", 0.0),
    )


def test_first_entry_sized_by_first_entry_pct_of_funds(tmp_path):
    snapshot = default_snapshot(available_funds=100_000)
    rm = make_risk_manager(tmp_path, snapshot, open_lots=0, first_entry_pct_of_funds=0.10)
    signal = make_signal(entry=10.0)

    decision = rm.evaluate(signal)

    assert decision.accepted
    assert decision.sized_qty == 1000  # floor(100_000 * 0.10 / 10)


def test_addon_entry_sized_by_smaller_addon_pct_of_funds(tmp_path):
    snapshot = default_snapshot(available_funds=100_000)
    rm = make_risk_manager(tmp_path, snapshot, open_lots=1, addon_pct_of_funds=0.05)
    signal = make_signal(entry=10.0)

    decision = rm.evaluate(signal)

    assert decision.accepted
    assert decision.sized_qty == 500  # floor(100_000 * 0.05 / 10)


def test_third_signal_on_same_symbol_rejected(tmp_path):
    snapshot = default_snapshot(available_funds=100_000)
    rm = make_risk_manager(tmp_path, snapshot, open_lots=2)
    signal = make_signal(entry=10.0)

    decision = rm.evaluate(signal)

    assert not decision.accepted
    assert decision.sized_qty == 0
    assert "already at max lots" in decision.reason
    assert "TEST" in decision.reason


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


def test_max_concurrent_positions_checked_before_lot_count(tmp_path):
    # Even a symbol with zero open lots of its own is rejected once the
    # account-wide concurrent-position cap is hit -- that check runs first.
    snapshot = default_snapshot(open_positions_count=3)
    rm = make_risk_manager(tmp_path, snapshot, open_lots=0, max_concurrent_positions=3)

    decision = rm.evaluate(make_signal())

    assert not decision.accepted
    assert "max concurrent positions" in decision.reason


def test_admits_top_tier_signal_into_reserved_slot_when_unrestricted_full(tmp_path):
    # 5 total, 1 reserved -> 4 unrestricted. All 4 taken; a scanner_rank 2
    # signal should still get the reserved 5th slot.
    snapshot = default_snapshot(open_positions_count=4)
    rm = make_risk_manager(
        tmp_path, snapshot, max_concurrent_positions=5, reserved_top_tier_slots=1, reserved_top_tier_max_rank=3
    )
    signal = make_signal(context={"scanner_rank": 2})

    decision = rm.evaluate(signal)

    assert decision.accepted


def test_rejects_low_rank_signal_when_only_reserved_slot_remains(tmp_path):
    snapshot = default_snapshot(open_positions_count=4)
    rm = make_risk_manager(
        tmp_path, snapshot, max_concurrent_positions=5, reserved_top_tier_slots=1, reserved_top_tier_max_rank=3
    )
    signal = make_signal(context={"scanner_rank": 10})

    decision = rm.evaluate(signal)

    assert not decision.accepted
    assert "reserved for scanner_rank" in decision.reason


def test_admits_any_rank_when_unrestricted_slots_still_open(tmp_path):
    snapshot = default_snapshot(open_positions_count=2)
    rm = make_risk_manager(
        tmp_path, snapshot, max_concurrent_positions=5, reserved_top_tier_slots=1, reserved_top_tier_max_rank=3
    )
    signal = make_signal(context={"scanner_rank": 25})

    decision = rm.evaluate(signal)

    assert decision.accepted


def test_rejects_all_when_fully_at_max_concurrent_positions_with_reserve(tmp_path):
    snapshot = default_snapshot(open_positions_count=5)
    rm = make_risk_manager(
        tmp_path, snapshot, max_concurrent_positions=5, reserved_top_tier_slots=1, reserved_top_tier_max_rank=3
    )
    signal = make_signal(context={"scanner_rank": 1})

    decision = rm.evaluate(signal)

    assert not decision.accepted
    assert "max concurrent positions reached (5)" in decision.reason


def test_missing_scanner_rank_ineligible_for_reserved_slot(tmp_path):
    snapshot = default_snapshot(open_positions_count=4)
    rm = make_risk_manager(
        tmp_path, snapshot, max_concurrent_positions=5, reserved_top_tier_slots=1, reserved_top_tier_max_rank=3
    )
    signal = make_signal(context={})  # no scanner_rank key at all

    decision = rm.evaluate(signal)

    assert not decision.accepted
    assert "reserved for scanner_rank" in decision.reason


def test_rejects_when_sized_qty_rounds_to_zero(tmp_path):
    snapshot = default_snapshot(available_funds=1)
    rm = make_risk_manager(tmp_path, snapshot, first_entry_pct_of_funds=0.10)
    signal = make_signal(entry=10.0)  # floor(1 * 0.10 / 10) = 0

    decision = rm.evaluate(signal)

    assert not decision.accepted
    assert "rounds to zero" in decision.reason


def test_sizing_capped_by_buying_power(tmp_path):
    snapshot = default_snapshot(available_funds=10_000_000, buying_power=100)
    rm = make_risk_manager(
        tmp_path, snapshot, first_entry_pct_of_funds=0.10, max_position_pct_of_buying_power=1.0
    )
    signal = make_signal(entry=10.0)

    decision = rm.evaluate(signal)

    assert decision.accepted
    assert decision.sized_qty == 10  # buying_power(100) / entry(10) -- the binding cap


def test_profit_cushion_reduces_size_before_goal_progress(tmp_path):
    snapshot = default_snapshot(available_funds=100_000, daily_realized_pnl=0.0)
    rm = make_risk_manager(
        tmp_path,
        snapshot,
        first_entry_pct_of_funds=0.10,
        daily_profit_goal_usd=1000,
        cushion_profit_fraction=0.25,
        cushion_size_fraction=0.25,
    )
    signal = make_signal(entry=10.0)

    decision = rm.evaluate(signal)

    assert decision.accepted
    # full size would be 1000; cushion not yet met (0 < 25% of 1000) -> 25%
    assert decision.sized_qty == 250


def test_profit_cushion_lifts_once_goal_fraction_realized(tmp_path):
    snapshot = default_snapshot(available_funds=100_000, daily_realized_pnl=300.0)  # >= 25% of 1000
    rm = make_risk_manager(
        tmp_path,
        snapshot,
        first_entry_pct_of_funds=0.10,
        daily_profit_goal_usd=1000,
        cushion_profit_fraction=0.25,
        cushion_size_fraction=0.25,
    )
    signal = make_signal(entry=10.0)

    decision = rm.evaluate(signal)

    assert decision.accepted
    assert decision.sized_qty == 1000


def test_profit_cushion_disabled_when_no_daily_goal_set(tmp_path):
    snapshot = default_snapshot(available_funds=100_000, daily_realized_pnl=0.0)
    rm = make_risk_manager(tmp_path, snapshot, first_entry_pct_of_funds=0.10, daily_profit_goal_usd=None)
    signal = make_signal(entry=10.0)

    decision = rm.evaluate(signal)

    assert decision.accepted
    assert decision.sized_qty == 1000


def _make_risk_manager_with_flag(tmp_path, snapshot, flatten_flag: bool, daily_loss_limit_pct=0.02) -> RiskManager:
    config = RiskConfig(
        daily_loss_limit_pct=daily_loss_limit_pct,
        flatten_on_daily_loss_limit=flatten_flag,
        max_concurrent_positions=3,
        max_position_pct_of_buying_power=0.25,
    )
    return RiskManager(config, FakeAccountState(snapshot), FakePositionManager(), kill_switch_path=tmp_path / "KILL_SWITCH")


def test_should_flatten_for_loss_limit_false_when_flag_disabled(tmp_path):
    snapshot = default_snapshot(net_liquidation=100_000, daily_realized_pnl=-2500)
    rm = _make_risk_manager_with_flag(tmp_path, snapshot, flatten_flag=False)
    rm.mark_start_of_day(100_000)

    assert rm.should_flatten_for_loss_limit(snapshot) is False


def test_should_flatten_for_loss_limit_false_when_under_threshold(tmp_path):
    snapshot = default_snapshot(net_liquidation=100_000, daily_realized_pnl=-500)
    rm = _make_risk_manager_with_flag(tmp_path, snapshot, flatten_flag=True, daily_loss_limit_pct=0.02)
    rm.mark_start_of_day(100_000)

    assert rm.should_flatten_for_loss_limit(snapshot) is False


def test_should_flatten_for_loss_limit_true_when_flag_enabled_and_breached(tmp_path):
    snapshot = default_snapshot(net_liquidation=100_000, daily_realized_pnl=-2500)
    rm = _make_risk_manager_with_flag(tmp_path, snapshot, flatten_flag=True, daily_loss_limit_pct=0.02)
    rm.mark_start_of_day(100_000)

    assert rm.should_flatten_for_loss_limit(snapshot) is True


def test_should_flatten_for_loss_limit_false_before_start_of_day_marked(tmp_path):
    snapshot = default_snapshot(net_liquidation=100_000, daily_realized_pnl=-2500)
    rm = _make_risk_manager_with_flag(tmp_path, snapshot, flatten_flag=True)

    assert rm.should_flatten_for_loss_limit(snapshot) is False


def test_start_of_day_equity_property(tmp_path):
    snapshot = default_snapshot(net_liquidation=100_000)
    rm = make_risk_manager(tmp_path, snapshot)

    assert rm.start_of_day_equity is None
    rm.mark_start_of_day(100_000)
    assert rm.start_of_day_equity == 100_000
