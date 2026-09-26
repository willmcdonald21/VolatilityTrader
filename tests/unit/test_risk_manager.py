from __future__ import annotations

from datetime import datetime, time, timezone

import pytest

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

    def __init__(
        self,
        open_lots: int = 0,
        tracked: set | None = None,
        first_entry_age: float | None = None,
        holder_strategies: set | None = None,
    ):
        self._open_lots = open_lots
        self._tracked = tracked or set()
        self._first_entry_age = first_entry_age
        # Defaults to "gap_and_go" (make_signal's own default strategy)
        # whenever a lot is open and no explicit holders were given -- the
        # common case in these tests is exercising the add-on path itself,
        # not the cross-strategy gate, so the default holder should look
        # like the signal's own strategy unless a test says otherwise.
        self._holder_strategies = holder_strategies if holder_strategies is not None else (
            {"gap_and_go"} if open_lots >= 1 else set()
        )

    def open_lot_count(self, symbol: str) -> int:
        return self._open_lots

    def open_lot_strategies(self, symbol: str) -> set:
        return set(self._holder_strategies)

    def tracked_symbols(self) -> set:
        return set(self._tracked)

    def seconds_since_first_entry(self, symbol: str):
        return self._first_entry_age


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
        risk_per_trade_pct=risk_overrides.get("risk_per_trade_pct"),
        addon_risk_pct=risk_overrides.get("addon_risk_pct"),
        max_stop_distance_pct=risk_overrides.get("max_stop_distance_pct", 2.0),
        allow_cross_strategy_stacking=risk_overrides.get("allow_cross_strategy_stacking", False),
        round_number_size_multiplier=risk_overrides.get("round_number_size_multiplier", 1.15),
        flat_top_size_multiplier=risk_overrides.get("flat_top_size_multiplier", 1.15),
    )
    account_state = FakeAccountState(snapshot)
    position_manager = FakePositionManager(open_lots)
    return RiskManager(
        config,
        account_state,
        position_manager,
        kill_switch_path=tmp_path / "KILL_SWITCH",
        no_entry_after_et=risk_overrides.get("no_entry_after_et"),
    )


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


def test_risk_based_sizing_derives_shares_from_stop_distance(tmp_path):
    snapshot = default_snapshot(net_liquidation=100_000, available_funds=100_000)
    rm = make_risk_manager(tmp_path, snapshot, risk_per_trade_pct=0.005)
    rm.mark_start_of_day(100_000)
    signal = make_signal(entry=2.00, stop=1.90)  # $0.10 of risk per share

    decision = rm.evaluate(signal)

    # $100k * 0.5% = $500 budget / $0.10 per share = 5,000 shares
    assert decision.sized_qty == 5000


def test_wider_stop_buys_fewer_shares_for_the_same_dollar_risk(tmp_path):
    # The entire point of the change: stop distance moves share count, not
    # the amount at risk. available_funds is generous so the notional cap
    # doesn't bind the tight-stop leg and hide the relationship.
    snapshot = default_snapshot(net_liquidation=100_000, available_funds=500_000)
    rm = make_risk_manager(tmp_path, snapshot, risk_per_trade_pct=0.005)
    rm.mark_start_of_day(100_000)

    tight = rm.evaluate(make_signal(entry=2.00, stop=1.96))  # $0.04 risk/share
    wide = rm.evaluate(make_signal(entry=2.00, stop=1.84))  # $0.16 risk/share

    assert tight.sized_qty == 12_500
    assert wide.sized_qty == 3_125
    for decision, risk_per_share in ((tight, 0.04), (wide, 0.16)):
        assert decision.sized_qty * risk_per_share == pytest.approx(500.0)


def test_notional_cap_still_binds_when_risk_budget_would_buy_more(tmp_path):
    # A very tight stop makes the risk budget enormous in share terms --
    # the %-of-funds cap is what stops it becoming an oversized position.
    snapshot = default_snapshot(net_liquidation=100_000, available_funds=100_000)
    rm = make_risk_manager(
        tmp_path, snapshot, risk_per_trade_pct=0.005, first_entry_pct_of_funds=0.10
    )
    rm.mark_start_of_day(100_000)
    signal = make_signal(entry=10.0, stop=9.99)  # $0.01 risk/share -> 50,000 shares by risk

    decision = rm.evaluate(signal)

    assert decision.sized_qty == 1000  # floor(100_000 * 0.10 / 10), the notional cap


def test_addon_lot_risks_less_than_a_first_entry(tmp_path):
    snapshot = default_snapshot(net_liquidation=100_000, available_funds=100_000)
    rm = make_risk_manager(
        tmp_path, snapshot, open_lots=1, risk_per_trade_pct=0.005, addon_risk_pct=0.0025
    )
    rm.mark_start_of_day(100_000)
    signal = make_signal(entry=2.00, stop=1.90)

    decision = rm.evaluate(signal)

    assert decision.sized_qty == 2500  # half the 5,000 a first entry would take


def test_risk_budget_uses_start_of_day_equity_not_the_drawn_down_snapshot(tmp_path):
    # Sizing off live equity would shrink every trade after a loss and
    # compound the drawdown; the budget stays fixed for the session.
    # available_funds deliberately generous so the notional cap can't bind
    # and mask which equity figure the risk budget used.
    snapshot = default_snapshot(net_liquidation=90_000, available_funds=500_000)
    rm = make_risk_manager(tmp_path, snapshot, risk_per_trade_pct=0.005)
    rm.mark_start_of_day(100_000)
    signal = make_signal(entry=2.00, stop=1.90)

    decision = rm.evaluate(signal)

    assert decision.sized_qty == 5000  # off 100k start-of-day, not the 90k now


def test_round_number_breakout_boosts_risk_based_size(tmp_path):
    # Generous available_funds/buying_power so the notional caps don't bind
    # and mask the multiplier's effect -- isolates the boost on the
    # risk-based figure specifically. Added 2026-09-26: round_number_breakout
    # was computed on every breakout-style signal well before this but never
    # actually wired to sizing anywhere.
    snapshot = default_snapshot(net_liquidation=100_000, available_funds=500_000, buying_power=1_000_000)
    rm = make_risk_manager(tmp_path, snapshot, risk_per_trade_pct=0.005, round_number_size_multiplier=1.15)
    rm.mark_start_of_day(100_000)
    signal = make_signal(entry=2.00, stop=1.96, context={"round_number_breakout": True})  # $0.04 risk/share -> 12,500 by risk

    decision = rm.evaluate(signal)

    assert decision.sized_qty == 14374  # floor(12,500 * 1.15) -- float residue (14374.999...) floors down one share


def test_size_multiplier_absent_when_context_flag_not_set(tmp_path):
    snapshot = default_snapshot(net_liquidation=100_000, available_funds=500_000, buying_power=1_000_000)
    rm = make_risk_manager(tmp_path, snapshot, risk_per_trade_pct=0.005, round_number_size_multiplier=1.15)
    rm.mark_start_of_day(100_000)
    signal = make_signal(entry=2.00, stop=1.96)  # no context at all

    decision = rm.evaluate(signal)

    assert decision.sized_qty == 12_500  # unboosted


def test_size_multiplier_never_exceeds_notional_cap(tmp_path):
    # Same numbers as test_notional_cap_still_binds_when_risk_budget_would_buy_more:
    # a very tight stop makes the risk budget enormous in share terms, and the
    # %-of-funds notional cap already binds even before any multiplier -- the
    # boost must not be a way to exceed that hard ceiling.
    snapshot = default_snapshot(net_liquidation=100_000, available_funds=100_000, buying_power=1_000_000)
    rm = make_risk_manager(
        tmp_path, snapshot, risk_per_trade_pct=0.005, first_entry_pct_of_funds=0.10, round_number_size_multiplier=1.15
    )
    rm.mark_start_of_day(100_000)
    signal = make_signal(entry=10.0, stop=9.99, context={"round_number_breakout": True})

    decision = rm.evaluate(signal)

    assert decision.sized_qty == 1000  # floor(100_000 * 0.10 / 10) -- unchanged, multiplier can't exceed this


def test_round_number_and_flat_top_multipliers_compound(tmp_path):
    snapshot = default_snapshot(net_liquidation=100_000, available_funds=500_000, buying_power=1_000_000)
    rm = make_risk_manager(
        tmp_path,
        snapshot,
        risk_per_trade_pct=0.005,
        round_number_size_multiplier=1.15,
        flat_top_size_multiplier=1.15,
    )
    rm.mark_start_of_day(100_000)
    signal = make_signal(
        entry=2.00, stop=1.96, context={"round_number_breakout": True, "flat_top_breakout": True}
    )

    decision = rm.evaluate(signal)

    assert decision.sized_qty == 16531  # floor(12,500 * 1.15 * 1.15)


def test_sizing_falls_back_to_notional_when_risk_sizing_is_disabled(tmp_path):
    snapshot = default_snapshot(available_funds=100_000)
    rm = make_risk_manager(tmp_path, snapshot, risk_per_trade_pct=None)
    signal = make_signal(entry=10.0, stop=9.0)

    decision = rm.evaluate(signal)

    assert decision.sized_qty == 1000  # unchanged legacy behaviour


# -- 2026-09-21 regressions --------------------------------------------------


def test_pending_brackets_count_toward_position_cap_before_any_fill(tmp_path):
    # Broker still reports zero positions (nothing has filled yet), but three
    # brackets were already submitted in the same second -- the cap is 3.
    snapshot = default_snapshot(open_positions_count=0)
    rm = make_risk_manager(tmp_path, snapshot, max_concurrent_positions=3)
    rm.position_manager = FakePositionManager(tracked={"AAA", "BBB", "CCC"})

    decision = rm.evaluate(make_signal())

    assert not decision.accepted
    assert "max concurrent positions" in decision.reason


def test_held_and_pending_symbols_are_deduplicated_in_the_cap(tmp_path):
    snapshot = default_snapshot(open_positions_count=1)
    snapshot.open_symbols = frozenset({"AAA"})
    rm = make_risk_manager(tmp_path, snapshot, max_concurrent_positions=3)
    rm.position_manager = FakePositionManager(tracked={"AAA", "BBB"})  # AAA counted once -> 2 open

    assert rm.evaluate(make_signal()).accepted


def test_unrealized_loss_counts_toward_daily_limit(tmp_path):
    snapshot = default_snapshot(net_liquidation=100_000, daily_realized_pnl=-500)
    snapshot.daily_unrealized_pnl = -1800  # -2300 total against a 2000 limit
    rm = make_risk_manager(tmp_path, snapshot, daily_loss_limit_pct=0.02)

    decision = rm.evaluate(make_signal())

    assert not decision.accepted
    assert "daily loss limit" in decision.reason
    assert rm.should_flatten_for_loss_limit(snapshot) is False  # flag off in this config; limit itself is breached
    assert rm._loss_limit_breached(snapshot)


def test_unrealized_gain_does_not_offset_realized_loss(tmp_path):
    snapshot = default_snapshot(net_liquidation=100_000, daily_realized_pnl=-2500)
    snapshot.daily_unrealized_pnl = 5000
    rm = make_risk_manager(tmp_path, snapshot, daily_loss_limit_pct=0.02)

    assert not rm.evaluate(make_signal()).accepted


def test_unrealized_loss_ignored_when_disabled(tmp_path):
    snapshot = default_snapshot(net_liquidation=100_000, daily_realized_pnl=0.0)
    snapshot.daily_unrealized_pnl = -5000
    rm = make_risk_manager(tmp_path, snapshot, daily_loss_limit_pct=0.02)
    rm.config.count_unrealized_loss_in_daily_limit = False

    assert rm.evaluate(make_signal()).accepted


def test_entry_rejected_before_window_opens(tmp_path):
    rm = make_risk_manager(tmp_path, default_snapshot())
    at_0400 = datetime(2026, 9, 21, 8, 0, 6, tzinfo=timezone.utc)  # 04:00:06 ET

    decision = rm.evaluate(make_signal(), now=at_0400)

    assert not decision.accepted
    assert "entry window not open until 04:15" in decision.reason


def test_entry_allowed_once_window_opens(tmp_path):
    rm = make_risk_manager(tmp_path, default_snapshot())
    at_0416 = datetime(2026, 9, 21, 8, 16, 0, tzinfo=timezone.utc)  # 04:16 ET

    assert rm.evaluate(make_signal(), now=at_0416).accepted


def test_no_clock_means_no_window_gate(tmp_path):
    rm = make_risk_manager(tmp_path, default_snapshot())

    assert rm.evaluate(make_signal()).accepted  # now omitted -> never guessed from wall-clock


def test_entry_rejected_at_or_after_eod_flatten_cutoff(tmp_path):
    # Confirmed live, 2026-09-21: AUUD signalled at 16:49 ET, an hour past
    # the 15:55 EOD flatten cutoff, filled, and had nothing left to close
    # it that day -- it sat open through the midnight reset into the next
    # morning's premarket.
    rm = make_risk_manager(tmp_path, default_snapshot(), no_entry_after_et=time(15, 55))
    at_1649 = datetime(2026, 9, 21, 20, 49, 0, tzinfo=timezone.utc)  # 16:49 ET

    decision = rm.evaluate(make_signal(), now=at_1649)

    assert not decision.accepted
    assert "entry window closed at 15:55" in decision.reason


def test_entry_allowed_right_up_to_the_eod_cutoff(tmp_path):
    rm = make_risk_manager(tmp_path, default_snapshot(), no_entry_after_et=time(15, 55))
    at_1554 = datetime(2026, 9, 21, 19, 54, 0, tzinfo=timezone.utc)  # 15:54 ET

    assert rm.evaluate(make_signal(), now=at_1554).accepted


def test_no_eod_cutoff_configured_means_no_upper_gate(tmp_path):
    rm = make_risk_manager(tmp_path, default_snapshot())  # no_entry_after_et defaults to None
    at_2000 = datetime(2026, 9, 22, 0, 0, 0, tzinfo=timezone.utc)  # 20:00 ET

    assert rm.evaluate(make_signal(), now=at_2000).accepted


def test_addon_rejected_when_first_lot_is_too_fresh(tmp_path):
    rm = make_risk_manager(tmp_path, default_snapshot(), open_lots=1)
    rm.position_manager = FakePositionManager(open_lots=1, first_entry_age=3.0)

    decision = rm.evaluate(make_signal())

    assert not decision.accepted
    assert "too soon after first entry" in decision.reason


def test_addon_allowed_after_minimum_gap(tmp_path):
    rm = make_risk_manager(tmp_path, default_snapshot(), open_lots=1)
    rm.position_manager = FakePositionManager(open_lots=1, first_entry_age=300.0)

    assert rm.evaluate(make_signal()).accepted


def test_cross_strategy_signal_rejected_when_symbol_held_by_different_strategy(tmp_path):
    # A symbol already open under vwap_reversion; a gap_and_go signal
    # (make_signal's default strategy) fires on the same name minutes
    # later. This is not a pyramid add-on -- it's a second, uncoordinated
    # strategy on a position it had no part in opening.
    rm = make_risk_manager(tmp_path, default_snapshot(), open_lots=1)
    rm.position_manager = FakePositionManager(
        open_lots=1, holder_strategies={"vwap_reversion"}, first_entry_age=300.0
    )

    decision = rm.evaluate(make_signal())

    assert not decision.accepted
    assert "cross_strategy_lot_conflict" in decision.reason
    assert "vwap_reversion" in decision.reason


def test_same_strategy_addon_not_blocked_by_cross_strategy_gate(tmp_path):
    # Every existing lot is the SAME strategy as the new signal -- this is
    # the ordinary pyramid add-on and must fall through untouched to the
    # existing age-based gate (and pass it here, at 300s old).
    rm = make_risk_manager(tmp_path, default_snapshot(), open_lots=1)
    rm.position_manager = FakePositionManager(
        open_lots=1, holder_strategies={"gap_and_go"}, first_entry_age=300.0
    )

    assert rm.evaluate(make_signal()).accepted


def test_cross_strategy_stacking_allowed_when_explicitly_enabled(tmp_path):
    rm = make_risk_manager(
        tmp_path, default_snapshot(), open_lots=1, allow_cross_strategy_stacking=True
    )
    rm.position_manager = FakePositionManager(
        open_lots=1, holder_strategies={"vwap_reversion"}, first_entry_age=300.0
    )

    assert rm.evaluate(make_signal()).accepted


def test_daily_loss_limit_halts_rest_of_day_even_after_pnl_recovers(tmp_path):
    # Confirmed live, 2026-09-22: the limit was breached and flattened
    # three separate times in one session because the old check re-derived
    # from live P&L on every signal -- a partial recovery silently reopened
    # the door. It must not: once breached, every later signal is rejected
    # for the rest of the trading day regardless of what P&L does next.
    snapshot = default_snapshot(net_liquidation=100_000, daily_realized_pnl=-2500)
    rm = make_risk_manager(tmp_path, snapshot, daily_loss_limit_pct=0.02)  # limit = 2000

    breached = rm.evaluate(make_signal())
    assert not breached.accepted
    assert "halted for the rest of the trading day" in breached.reason

    # P&L fully recovers to flat -- old behavior would accept again.
    rm.account_state = FakeAccountState(default_snapshot(net_liquidation=100_000, daily_realized_pnl=0.0))
    recovered = rm.evaluate(make_signal())

    assert not recovered.accepted
    assert "halted for the rest of the trading day" in recovered.reason


def test_daily_loss_limit_halt_clears_on_mark_start_of_day(tmp_path):
    snapshot = default_snapshot(net_liquidation=100_000, daily_realized_pnl=-2500)
    rm = make_risk_manager(tmp_path, snapshot, daily_loss_limit_pct=0.02)
    assert not rm.evaluate(make_signal()).accepted
    assert rm._loss_limit_halted_today is True

    rm.account_state = FakeAccountState(default_snapshot(net_liquidation=100_000, daily_realized_pnl=0.0))
    rm.mark_start_of_day(100_000)  # simulates reset_daily_state() on a new trading day

    assert rm._loss_limit_halted_today is False
    assert rm.evaluate(make_signal()).accepted


def test_no_loss_limit_halt_when_never_breached(tmp_path):
    rm = make_risk_manager(tmp_path, default_snapshot(daily_realized_pnl=0.0), daily_loss_limit_pct=0.02)

    assert rm.evaluate(make_signal()).accepted
    assert rm._loss_limit_halted_today is False


def test_load_state_restores_equity_and_halt_without_clearing_it(tmp_path):
    # The entire point of load_state vs. mark_start_of_day: restoring a
    # persisted halt must not un-halt it.
    rm = make_risk_manager(tmp_path, default_snapshot(net_liquidation=100_000, daily_realized_pnl=0.0))

    rm.load_state(start_of_day_equity=100_000.0, loss_limit_halted=True)

    assert rm.start_of_day_equity == 100_000.0
    assert rm.loss_limit_halted_today is True
    assert not rm.evaluate(make_signal()).accepted


def test_load_state_restores_a_clean_unhalted_day_too(tmp_path):
    rm = make_risk_manager(tmp_path, default_snapshot(net_liquidation=100_000, daily_realized_pnl=0.0))

    rm.load_state(start_of_day_equity=100_000.0, loss_limit_halted=False)

    assert rm.loss_limit_halted_today is False
    assert rm.evaluate(make_signal()).accepted


def test_mark_start_of_day_always_clears_the_halt(tmp_path):
    snapshot = default_snapshot(net_liquidation=100_000, daily_realized_pnl=-2500)
    rm = make_risk_manager(tmp_path, snapshot, daily_loss_limit_pct=0.02)
    assert not rm.evaluate(make_signal()).accepted
    assert rm.loss_limit_halted_today is True

    rm.account_state = FakeAccountState(default_snapshot(net_liquidation=100_000, daily_realized_pnl=0.0))
    rm.mark_start_of_day(100_000.0)  # a genuine new trading day, not a same-day restart

    assert rm.loss_limit_halted_today is False
    assert rm.evaluate(make_signal()).accepted
