from __future__ import annotations

from types import SimpleNamespace

from warrior_bot.utils import panic


class FakeContract:
    def __init__(self, symbol: str, exchange: str = "NASDAQ"):
        self.symbol = symbol
        self.exchange = exchange


class FakeIB:
    def __init__(self, positions, notifications_enabled=False):
        self._positions = positions
        self.placed = []

    def positions(self, account=""):
        return self._positions

    def placeOrder(self, contract, order):
        self.placed.append((contract, order))

    def reqGlobalCancel(self):
        pass


def make_position(symbol: str, qty: float, exchange: str = "NASDAQ"):
    return SimpleNamespace(contract=FakeContract(symbol, exchange), position=qty)


def test_flatten_routes_through_smart_not_direct_exchange(monkeypatch):
    monkeypatch.setattr(panic, "alert", lambda *a, **k: None)
    original_contract = FakeContract("BIRD", exchange="NASDAQ")
    ib = FakeIB([SimpleNamespace(contract=original_contract, position=108872.0)])

    panic.flatten_all_positions(ib)

    assert len(ib.placed) == 1
    placed_contract, order = ib.placed[0]
    assert placed_contract.exchange == "SMART"
    assert order.action == "SELL"
    assert order.totalQuantity == 108872.0


def test_flatten_does_not_mutate_original_position_contract(monkeypatch):
    monkeypatch.setattr(panic, "alert", lambda *a, **k: None)
    original_contract = FakeContract("BIRD", exchange="NASDAQ")
    ib = FakeIB([SimpleNamespace(contract=original_contract, position=108872.0)])

    panic.flatten_all_positions(ib)

    assert original_contract.exchange == "NASDAQ"  # untouched -- placeOrder got a copy


def test_flatten_skips_zero_qty_positions(monkeypatch):
    monkeypatch.setattr(panic, "alert", lambda *a, **k: None)
    ib = FakeIB([make_position("FLAT", 0.0)])

    panic.flatten_all_positions(ib)

    assert ib.placed == []


def test_flatten_buys_to_close_short_positions(monkeypatch):
    monkeypatch.setattr(panic, "alert", lambda *a, **k: None)
    ib = FakeIB([make_position("SHRT", -500.0)])

    panic.flatten_all_positions(ib)

    _, order = ib.placed[0]
    assert order.action == "BUY"
    assert order.totalQuantity == 500.0
