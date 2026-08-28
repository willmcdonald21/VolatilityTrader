from __future__ import annotations

from datetime import datetime, timezone

from warrior_bot.execution.bracket_builder import build_bracket
from warrior_bot.signals.signal import Signal


class FakeClient:
    def __init__(self):
        self._next_id = 1

    def getReqId(self) -> int:
        rid = self._next_id
        self._next_id += 1
        return rid


class FakeIB:
    def __init__(self):
        self.client = FakeClient()


def make_signal() -> Signal:
    return Signal(
        symbol="TEST",
        strategy="gap_and_go",
        side="BUY",
        entry_price=10.0,
        stop_price=9.0,
        target_price=12.0,
        ts=datetime.now(timezone.utc),
    )


def test_transmit_flags_only_last_leg_transmits():
    bracket = build_bracket(FakeIB(), make_signal(), quantity=100)
    assert bracket.parent.transmit is False
    assert bracket.take_profits[0].transmit is False
    assert bracket.stop_loss.transmit is True


def test_children_link_to_parent_order_id():
    bracket = build_bracket(FakeIB(), make_signal(), quantity=100)
    assert bracket.take_profits[0].parentId == bracket.parent.orderId
    assert bracket.stop_loss.parentId == bracket.parent.orderId


def test_exit_legs_are_oca_linked_for_cancel_on_fill():
    bracket = build_bracket(FakeIB(), make_signal(), quantity=100)
    assert bracket.take_profits[0].ocaGroup == bracket.stop_loss.ocaGroup
    assert bracket.take_profits[0].ocaGroup != ""
    assert bracket.take_profits[0].ocaType == 1
    assert bracket.stop_loss.ocaType == 1
    # the parent (entry) order must NOT be part of the exit OCA group
    assert bracket.parent.ocaGroup != bracket.take_profits[0].ocaGroup


def test_actions_and_prices_match_signal():
    bracket = build_bracket(FakeIB(), make_signal(), quantity=100)
    assert bracket.parent.action == "BUY"
    assert bracket.take_profits[0].action == "SELL"
    assert bracket.stop_loss.action == "SELL"
    assert bracket.parent.lmtPrice == 10.0
    assert bracket.take_profits[0].lmtPrice == 12.0
    assert bracket.stop_loss.auxPrice == 9.0
    assert bracket.parent.totalQuantity == 100


def test_no_naked_entry_default_fallback_is_three_orders():
    bracket = build_bracket(FakeIB(), make_signal(), quantity=100)
    assert len(bracket.orders) == 3


def test_orders_allowed_outside_regular_trading_hours():
    bracket = build_bracket(FakeIB(), make_signal(), quantity=100)
    assert bracket.parent.outsideRth is True
    assert bracket.take_profits[0].outsideRth is True
    assert bracket.stop_loss.outsideRth is True


def test_orders_have_explicit_day_tif():
    bracket = build_bracket(FakeIB(), make_signal(), quantity=100)
    assert bracket.parent.tif == "DAY"
    assert bracket.take_profits[0].tif == "DAY"
    assert bracket.stop_loss.tif == "DAY"


def test_default_bracket_target_role_is_target_when_no_tiers_given():
    bracket = build_bracket(FakeIB(), make_signal(), quantity=100)
    assert bracket.target_roles == ["target"]
    assert bracket.take_profits[0].totalQuantity == 100


def test_profit_tiers_build_one_independent_leg_per_tier():
    bracket = build_bracket(
        FakeIB(), make_signal(), quantity=100, profit_tiers=[(34, 10.5), (33, 11.0)]
    )
    assert bracket.target_roles == ["scale_out", "scale_out"]
    assert len(bracket.take_profits) == 2
    assert bracket.take_profits[0].totalQuantity == 34
    assert bracket.take_profits[0].lmtPrice == 10.5
    assert bracket.take_profits[1].totalQuantity == 33
    assert bracket.take_profits[1].lmtPrice == 11.0
    # stop still protects the FULL quantity up front -- resizing happens
    # reactively (in PositionManager) only after each tier's fill
    assert bracket.stop_loss.totalQuantity == 100
    # partial legs are never OCA'd with the stop (would cancel the
    # full-quantity stop the instant the first, smaller tier fills)
    assert bracket.take_profits[0].ocaGroup == ""
    assert bracket.take_profits[1].ocaGroup == ""
    assert bracket.stop_loss.ocaGroup == ""
    assert len(bracket.orders) == 4  # parent + 2 tiers + stop


def test_profit_tier_out_of_bounds_qty_is_dropped():
    bracket = build_bracket(
        FakeIB(), make_signal(), quantity=100, profit_tiers=[(150, 10.5)]  # exceeds quantity -- invalid
    )
    # no valid tiers left -- falls back to the default single full-qty target
    assert bracket.target_roles == ["target"]
    assert bracket.take_profits[0].totalQuantity == 100
    assert bracket.take_profits[0].ocaGroup != ""


def test_empty_profit_tiers_falls_back_to_full_qty_target():
    bracket = build_bracket(FakeIB(), make_signal(), quantity=100, profit_tiers=[])
    assert bracket.target_roles == ["target"]
    assert bracket.take_profits[0].totalQuantity == 100
    assert bracket.take_profits[0].ocaGroup != ""


def test_stop_loss_is_a_stop_limit_order():
    # IBKR rejects plain market orders (what a triggered STP resolves to)
    # outside regular trading hours -- STP LMT keeps the stop working
    # pre-market/after-hours.
    bracket = build_bracket(FakeIB(), make_signal(), quantity=100)
    assert bracket.stop_loss.orderType == "STP LMT"
    assert bracket.stop_loss.auxPrice == 9.0  # trigger unchanged


def test_stop_loss_limit_sits_below_trigger_by_configured_offset():
    bracket = build_bracket(FakeIB(), make_signal(), quantity=100, stop_limit_offset_pct=1.0)
    assert bracket.stop_loss.action == "SELL"
    assert bracket.stop_loss.lmtPrice == 9.0 * 0.99


def test_stop_loss_offset_defaults_to_half_percent():
    bracket = build_bracket(FakeIB(), make_signal(), quantity=100)
    assert bracket.stop_loss.lmtPrice == 9.0 * 0.995
