from __future__ import annotations

from datetime import datetime, timezone

from warrior_bot.config import RiskConfig
from warrior_bot.risk.account_state import AccountSnapshot
from warrior_bot.risk.risk_manager import RiskManager
from warrior_bot.signals.signal import Signal


class FakeAccountState:
    def __init__(self, snapshot: AccountSnapshot, first_closing_trade_pnl: float | None = None):
        self._snapshot = snapshot
        self._first_closing_trade_pnl = first_closing_trade_pnl

    def snapshot(self) -> AccountSnapshot:
        return self._snapshot

    def first_closing_trade_pnl(self) -> float | None:
        return self._first_closing_trade_pnl


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


def make_risk_manager(tmp_path, snapshot, **risk_overrides) -> RiskManager:
    config = RiskConfig(
        risk_per_trade_pct=risk_overrides.get("risk_per_trade_pct", 0.01),
        daily_loss_limit_pct=risk_overrides.get("daily_loss_limit_pct", 0.02),
        max_concurrent_positions=risk_overrides.get("max_concurrent_positions", 3),
        max_position_notional_usd=risk_overrides.get("max_position_notional_usd", 5000),
        max_shares_per_trade=risk_overrides.get("max_shares_per_trade", 2000),
        daily_profit_goal_usd=risk_overrides.get("daily_profit_goal_usd"),
        cushion_profit_fraction=risk_overrides.get("cushion_profit_fraction", 0.25),
        cushion_size_fraction=risk_overrides.get("cushion_size_fraction", 0.25),
        catalyst_size_multiplier=risk_overrides.get("catalyst_size_multiplier", 1.25),
        obvious_rank_threshold=risk_overrides.get("obvious_rank_threshold", 3),
        obvious_size_multiplier=risk_overrides.get("obvious_size_multiplier", 1.25),
        shallow_pullback_threshold_pct=risk_overrides.get("shallow_pullback_threshold_pct", 25.0),
        shallow_pullback_size_multiplier=risk_overrides.get("shallow_pullback_size_multiplier", 1.25),
        bottoming_tail_size_multiplier=risk_overrides.get("bottoming_tail_size_multiplier", 1.25),
        round_number_size_multiplier=risk_overrides.get("round_number_size_multiplier", 1.25),
        # Unlike every other multiplier above, the starter-trade ones are
        # gated on RiskManager's own internal state (this being the day's
        # first trade), not on something present/absent in the signal's
        # context -- so unless a test opts in, every other test in this
        # file's first `evaluate()` call would otherwise silently hit the
        # starter-trade-size branch. Default to a no-op (1.0) here; real
        # production default (0.5) lives in RiskConfig itself.
        starter_trade_size_multiplier=risk_overrides.get("starter_trade_size_multiplier", 1.0),
        starter_trade_downgrade_multiplier=risk_overrides.get("starter_trade_downgrade_multiplier", 1.0),
    )
    account_state = FakeAccountState(snapshot, first_closing_trade_pnl=risk_overrides.get("first_closing_trade_pnl"))
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


def test_catalyst_signal_gets_size_boost(tmp_path):
    snapshot = default_snapshot(net_liquidation=10_000)
    rm = make_risk_manager(tmp_path, snapshot, risk_per_trade_pct=0.01, catalyst_size_multiplier=1.25)
    signal = make_signal(entry=10.0, stop=9.0, context={"catalyst_category": "earnings"})

    decision = rm.evaluate(signal)

    assert decision.accepted
    assert decision.sized_qty == 125  # raw_shares(100) * 1.25


def test_no_catalyst_no_size_boost(tmp_path):
    snapshot = default_snapshot(net_liquidation=10_000)
    rm = make_risk_manager(tmp_path, snapshot, risk_per_trade_pct=0.01, catalyst_size_multiplier=1.25)
    signal = make_signal(entry=10.0, stop=9.0)  # no catalyst in context

    decision = rm.evaluate(signal)

    assert decision.accepted
    assert decision.sized_qty == 100  # unboosted


def test_catalyst_boost_still_capped_by_hard_limits(tmp_path):
    snapshot = default_snapshot(net_liquidation=10_000)
    rm = make_risk_manager(tmp_path, snapshot, risk_per_trade_pct=0.01, catalyst_size_multiplier=100)
    signal = make_signal(entry=10.0, stop=9.0, context={"catalyst_category": "merger"})

    decision = rm.evaluate(signal)

    assert decision.accepted
    # raw_shares(100) * 100 = 10,000 -- but still clamped to max_position_notional_usd(5000)/entry(10)
    assert decision.sized_qty == 500


def test_obvious_rank_signal_gets_size_boost(tmp_path):
    snapshot = default_snapshot(net_liquidation=10_000)
    rm = make_risk_manager(tmp_path, snapshot, risk_per_trade_pct=0.01, obvious_rank_threshold=3, obvious_size_multiplier=1.25)
    signal = make_signal(entry=10.0, stop=9.0, context={"scanner_rank": 1})

    decision = rm.evaluate(signal)

    assert decision.accepted
    assert decision.sized_qty == 125  # raw_shares(100) * 1.25


def test_rank_outside_obvious_threshold_no_size_boost(tmp_path):
    snapshot = default_snapshot(net_liquidation=10_000)
    rm = make_risk_manager(tmp_path, snapshot, risk_per_trade_pct=0.01, obvious_rank_threshold=3, obvious_size_multiplier=1.25)
    signal = make_signal(entry=10.0, stop=9.0, context={"scanner_rank": 10})

    decision = rm.evaluate(signal)

    assert decision.accepted
    assert decision.sized_qty == 100  # unboosted -- rank 10 is outside the top-3 threshold


def test_no_scanner_rank_no_size_boost(tmp_path):
    snapshot = default_snapshot(net_liquidation=10_000)
    rm = make_risk_manager(tmp_path, snapshot, risk_per_trade_pct=0.01)
    signal = make_signal(entry=10.0, stop=9.0)  # no scanner_rank in context

    decision = rm.evaluate(signal)

    assert decision.accepted
    assert decision.sized_qty == 100


def test_shallow_pullback_signal_gets_size_boost(tmp_path):
    snapshot = default_snapshot(net_liquidation=10_000)
    rm = make_risk_manager(tmp_path, snapshot, risk_per_trade_pct=0.01, shallow_pullback_threshold_pct=25.0, shallow_pullback_size_multiplier=1.25)
    signal = make_signal(entry=10.0, stop=9.0, context={"pullback_pct": 10.0})  # well within the top-25% band

    decision = rm.evaluate(signal)

    assert decision.accepted
    assert decision.sized_qty == 125  # raw_shares(100) * 1.25


def test_deep_but_valid_pullback_no_size_boost(tmp_path):
    snapshot = default_snapshot(net_liquidation=10_000)
    rm = make_risk_manager(tmp_path, snapshot, risk_per_trade_pct=0.01, shallow_pullback_threshold_pct=25.0, shallow_pullback_size_multiplier=1.25)
    signal = make_signal(entry=10.0, stop=9.0, context={"pullback_pct": 40.0})  # valid but deeper than the shallow threshold

    decision = rm.evaluate(signal)

    assert decision.accepted
    assert decision.sized_qty == 100  # unboosted


def test_no_pullback_pct_no_size_boost(tmp_path):
    snapshot = default_snapshot(net_liquidation=10_000)
    rm = make_risk_manager(tmp_path, snapshot, risk_per_trade_pct=0.01)
    signal = make_signal(entry=10.0, stop=9.0)  # no pullback_pct in context (e.g. a gap_and_go signal)

    decision = rm.evaluate(signal)

    assert decision.accepted
    assert decision.sized_qty == 100


def test_first_trade_of_day_gets_starter_size_reduction(tmp_path):
    snapshot = default_snapshot(net_liquidation=10_000)
    rm = make_risk_manager(tmp_path, snapshot, risk_per_trade_pct=0.01, starter_trade_size_multiplier=0.5)
    signal = make_signal(entry=10.0, stop=9.0)

    decision = rm.evaluate(signal)

    assert decision.accepted
    assert decision.sized_qty == 50  # raw_shares(100) * 0.5


def test_second_trade_of_day_not_starter_sized(tmp_path):
    snapshot = default_snapshot(net_liquidation=10_000)
    rm = make_risk_manager(tmp_path, snapshot, risk_per_trade_pct=0.01, starter_trade_size_multiplier=0.5)
    rm.evaluate(make_signal(entry=10.0, stop=9.0))  # trade #1 -- consumes the starter-size branch

    decision = rm.evaluate(make_signal(entry=10.0, stop=9.0))  # trade #2

    assert decision.accepted
    assert decision.sized_qty == 100  # full size -- starter trade hasn't resolved (still open) yet


def test_regime_downgrade_applied_after_starter_trade_loses(tmp_path):
    snapshot = default_snapshot(net_liquidation=10_000)
    rm = make_risk_manager(
        tmp_path,
        snapshot,
        risk_per_trade_pct=0.01,
        starter_trade_downgrade_multiplier=0.5,
        first_closing_trade_pnl=-50.0,  # the starter trade already closed at a loss
    )
    rm.evaluate(make_signal(entry=10.0, stop=9.0))  # trade #1 -- the (now-resolved) starter trade

    decision = rm.evaluate(make_signal(entry=10.0, stop=9.0))  # trade #2

    assert decision.accepted
    assert decision.sized_qty == 50  # raw_shares(100) * 0.5 -- cold-market caution flag


def test_no_regime_downgrade_after_starter_trade_wins(tmp_path):
    snapshot = default_snapshot(net_liquidation=10_000)
    rm = make_risk_manager(
        tmp_path,
        snapshot,
        risk_per_trade_pct=0.01,
        starter_trade_downgrade_multiplier=0.5,
        first_closing_trade_pnl=50.0,  # the starter trade closed profitably
    )
    rm.evaluate(make_signal(entry=10.0, stop=9.0))  # trade #1

    decision = rm.evaluate(make_signal(entry=10.0, stop=9.0))  # trade #2

    assert decision.accepted
    assert decision.sized_qty == 100  # unboosted, unreduced -- starter trade worked


def test_no_regime_downgrade_while_starter_trade_still_open(tmp_path):
    snapshot = default_snapshot(net_liquidation=10_000)
    rm = make_risk_manager(
        tmp_path,
        snapshot,
        risk_per_trade_pct=0.01,
        starter_trade_downgrade_multiplier=0.5,
        first_closing_trade_pnl=None,  # nothing has closed yet
    )
    rm.evaluate(make_signal(entry=10.0, stop=9.0))  # trade #1

    decision = rm.evaluate(make_signal(entry=10.0, stop=9.0))  # trade #2

    assert decision.accepted
    assert decision.sized_qty == 100


def test_starter_trade_state_resets_on_mark_start_of_day(tmp_path):
    snapshot = default_snapshot(net_liquidation=10_000)
    rm = make_risk_manager(tmp_path, snapshot, risk_per_trade_pct=0.01, starter_trade_size_multiplier=0.5)
    rm.evaluate(make_signal(entry=10.0, stop=9.0))  # trade #1 consumes the starter slot

    rm.mark_start_of_day(10_000)  # new day
    decision = rm.evaluate(make_signal(entry=10.0, stop=9.0))

    assert decision.accepted
    assert decision.sized_qty == 50  # starter-size branch applies again on the new day's trade #1


def test_bottoming_tail_confirmation_gets_size_boost(tmp_path):
    snapshot = default_snapshot(net_liquidation=10_000)
    rm = make_risk_manager(tmp_path, snapshot, risk_per_trade_pct=0.01, bottoming_tail_size_multiplier=1.25)
    signal = make_signal(entry=10.0, stop=9.0, context={"bottoming_tail_confirmation": True})

    decision = rm.evaluate(signal)

    assert decision.accepted
    assert decision.sized_qty == 125  # raw_shares(100) * 1.25


def test_no_bottoming_tail_no_size_boost(tmp_path):
    snapshot = default_snapshot(net_liquidation=10_000)
    rm = make_risk_manager(tmp_path, snapshot, risk_per_trade_pct=0.01, bottoming_tail_size_multiplier=1.25)
    signal = make_signal(entry=10.0, stop=9.0, context={"bottoming_tail_confirmation": False})

    decision = rm.evaluate(signal)

    assert decision.accepted
    assert decision.sized_qty == 100  # unboosted


def test_round_number_breakout_gets_size_boost(tmp_path):
    snapshot = default_snapshot(net_liquidation=10_000)
    rm = make_risk_manager(tmp_path, snapshot, risk_per_trade_pct=0.01, round_number_size_multiplier=1.25)
    signal = make_signal(entry=10.0, stop=9.0, context={"round_number_breakout": True})

    decision = rm.evaluate(signal)

    assert decision.accepted
    assert decision.sized_qty == 125  # raw_shares(100) * 1.25


def test_no_round_number_breakout_no_size_boost(tmp_path):
    snapshot = default_snapshot(net_liquidation=10_000)
    rm = make_risk_manager(tmp_path, snapshot, risk_per_trade_pct=0.01, round_number_size_multiplier=1.25)
    signal = make_signal(entry=10.0, stop=9.0, context={"round_number_breakout": False})

    decision = rm.evaluate(signal)

    assert decision.accepted
    assert decision.sized_qty == 100  # unboosted


def test_time_of_day_boost_applied_within_window(tmp_path):
    snapshot = default_snapshot(net_liquidation=10_000)
    rm = make_risk_manager(tmp_path, snapshot, risk_per_trade_pct=0.01)
    signal = make_signal(entry=10.0, stop=9.0)
    now = datetime(2026, 1, 5, 12, 30, tzinfo=timezone.utc)  # 07:30 ET (EST, UTC-5) -- inside 07:00-10:00

    decision = rm.evaluate(signal, now=now)

    assert decision.accepted
    assert decision.sized_qty == 125  # raw_shares(100) * 1.25


def test_time_of_day_boost_not_applied_outside_window(tmp_path):
    snapshot = default_snapshot(net_liquidation=10_000)
    rm = make_risk_manager(tmp_path, snapshot, risk_per_trade_pct=0.01)
    signal = make_signal(entry=10.0, stop=9.0)
    now = datetime(2026, 1, 5, 20, 0, tzinfo=timezone.utc)  # 15:00 ET -- outside the window

    decision = rm.evaluate(signal, now=now)

    assert decision.accepted
    assert decision.sized_qty == 100  # unboosted


def test_time_of_day_boost_not_applied_when_now_not_supplied(tmp_path):
    snapshot = default_snapshot(net_liquidation=10_000)
    rm = make_risk_manager(tmp_path, snapshot, risk_per_trade_pct=0.01)
    signal = make_signal(entry=10.0, stop=9.0)

    decision = rm.evaluate(signal)  # no `now` -- must never guess from wall-clock time

    assert decision.accepted
    assert decision.sized_qty == 100


def test_time_of_day_and_catalyst_boosts_stack(tmp_path):
    snapshot = default_snapshot(net_liquidation=10_000)
    rm = make_risk_manager(tmp_path, snapshot, risk_per_trade_pct=0.01, catalyst_size_multiplier=1.25)
    signal = make_signal(entry=10.0, stop=9.0, context={"catalyst_category": "earnings"})
    now = datetime(2026, 1, 5, 12, 30, tzinfo=timezone.utc)  # inside the boost window

    decision = rm.evaluate(signal, now=now)

    assert decision.accepted
    assert decision.sized_qty == 156  # floor(floor(100*1.25)*1.25) = floor(125*1.25) = floor(156.25) = 156


def test_profit_cushion_reduces_size_before_goal_progress(tmp_path):
    snapshot = default_snapshot(net_liquidation=100_000, daily_realized_pnl=0.0)
    rm = make_risk_manager(
        tmp_path, snapshot, risk_per_trade_pct=0.01, daily_profit_goal_usd=1000, cushion_profit_fraction=0.25, cushion_size_fraction=0.25
    )
    signal = make_signal(entry=10.0, stop=9.0)

    decision = rm.evaluate(signal)

    assert decision.accepted
    # full size would be 500 (capped by max_position_notional_usd); cushion not yet met -> 25%
    assert decision.sized_qty == 125


def test_profit_cushion_lifts_once_goal_fraction_realized(tmp_path):
    snapshot = default_snapshot(net_liquidation=100_000, daily_realized_pnl=300.0)  # >= 25% of 1000
    rm = make_risk_manager(
        tmp_path, snapshot, risk_per_trade_pct=0.01, daily_profit_goal_usd=1000, cushion_profit_fraction=0.25, cushion_size_fraction=0.25
    )
    signal = make_signal(entry=10.0, stop=9.0)

    decision = rm.evaluate(signal)

    assert decision.accepted
    assert decision.sized_qty == 500


def test_profit_cushion_disabled_when_no_daily_goal_set(tmp_path):
    snapshot = default_snapshot(net_liquidation=100_000, daily_realized_pnl=0.0)
    rm = make_risk_manager(tmp_path, snapshot, risk_per_trade_pct=0.01, daily_profit_goal_usd=None)
    signal = make_signal(entry=10.0, stop=9.0)

    decision = rm.evaluate(signal)

    assert decision.accepted
    assert decision.sized_qty == 500


def _make_risk_manager_with_flag(tmp_path, snapshot, flatten_flag: bool, daily_loss_limit_pct=0.02) -> RiskManager:
    config = RiskConfig(
        risk_per_trade_pct=0.01,
        daily_loss_limit_pct=daily_loss_limit_pct,
        flatten_on_daily_loss_limit=flatten_flag,
        max_concurrent_positions=3,
        max_position_notional_usd=5000,
        max_shares_per_trade=2000,
    )
    return RiskManager(config, FakeAccountState(snapshot), kill_switch_path=tmp_path / "KILL_SWITCH")


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
