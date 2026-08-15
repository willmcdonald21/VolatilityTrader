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
    assert bracket.take_profit.transmit is False
    assert bracket.stop_loss.transmit is True


def test_children_link_to_parent_order_id():
    bracket = build_bracket(FakeIB(), make_signal(), quantity=100)
    assert bracket.take_profit.parentId == bracket.parent.orderId
    assert bracket.stop_loss.parentId == bracket.parent.orderId


def test_exit_legs_are_oca_linked_for_cancel_on_fill():
    bracket = build_bracket(FakeIB(), make_signal(), quantity=100)
    assert bracket.take_profit.ocaGroup == bracket.stop_loss.ocaGroup
    assert bracket.take_profit.ocaGroup != ""
    assert bracket.take_profit.ocaType == 1
    assert bracket.stop_loss.ocaType == 1
    # the parent (entry) order must NOT be part of the exit OCA group
    assert bracket.parent.ocaGroup != bracket.take_profit.ocaGroup


def test_actions_and_prices_match_signal():
    bracket = build_bracket(FakeIB(), make_signal(), quantity=100)
    assert bracket.parent.action == "BUY"
    assert bracket.take_profit.action == "SELL"
    assert bracket.stop_loss.action == "SELL"
    assert bracket.parent.lmtPrice == 10.0
    assert bracket.take_profit.lmtPrice == 12.0
    assert bracket.stop_loss.auxPrice == 9.0
    assert bracket.parent.totalQuantity == 100


def test_no_naked_entry_all_three_orders_present():
    bracket = build_bracket(FakeIB(), make_signal(), quantity=100)
    assert len(bracket.orders) == 3


def test_orders_allowed_outside_regular_trading_hours():
    bracket = build_bracket(FakeIB(), make_signal(), quantity=100)
    assert bracket.parent.outsideRth is True
    assert bracket.take_profit.outsideRth is True
    assert bracket.stop_loss.outsideRth is True


def test_orders_have_explicit_day_tif():
    bracket = build_bracket(FakeIB(), make_signal(), quantity=100)
    assert bracket.parent.tif == "DAY"
    assert bracket.take_profit.tif == "DAY"
    assert bracket.stop_loss.tif == "DAY"
