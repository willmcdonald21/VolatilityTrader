from __future__ import annotations

from types import SimpleNamespace

from warrior_bot.risk.account_state import AccountState


def make_position(symbol: str, qty: float):
    return SimpleNamespace(contract=SimpleNamespace(symbol=symbol), position=qty)


class FakeIB:
    def __init__(self, positions):
        self._positions = positions

    def positions(self, account=""):
        return self._positions


def test_has_open_position_true_for_held_symbol():
    ib = FakeIB([make_position("BIRD", 108872.0)])
    account_state = AccountState(ib)

    assert account_state.has_open_position("BIRD") is True


def test_has_open_position_false_for_unheld_symbol():
    ib = FakeIB([make_position("BIRD", 108872.0)])
    account_state = AccountState(ib)

    assert account_state.has_open_position("BMRA") is False


def test_has_open_position_false_for_zero_qty_position():
    # IBKR can still list a symbol with position=0 (fully closed but not
    # yet pruned from the positions() snapshot) -- must not count as "held".
    ib = FakeIB([make_position("BIRD", 0.0)])
    account_state = AccountState(ib)

    assert account_state.has_open_position("BIRD") is False


def test_has_open_position_false_with_no_positions():
    ib = FakeIB([])
    account_state = AccountState(ib)

    assert account_state.has_open_position("BIRD") is False
