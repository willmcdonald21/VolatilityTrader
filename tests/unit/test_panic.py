from __future__ import annotations

from types import SimpleNamespace

from warrior_bot.utils import panic


class FakeContract:
    def __init__(self, symbol: str, exchange: str = "NASDAQ"):
        self.symbol = symbol
        self.exchange = exchange


class FakeIB:
    def __init__(self, positions, notifications_enabled=False, open_trades=None, portfolio=None):
        self._positions = positions
        self.placed = []
        self._open_trades = open_trades or []
        self._portfolio = portfolio or []

    def openTrades(self):
        return self._open_trades

    def portfolio(self, account=""):
        return self._portfolio

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


def test_flatten_position_flattens_a_single_symbol(monkeypatch):
    monkeypatch.setattr(panic, "alert", lambda *a, **k: None)
    ib = FakeIB([])  # deliberately not driven from ib.positions() -- caller already has the position

    panic.flatten_position(ib, make_position("NRXS", -850.0))

    assert len(ib.placed) == 1
    contract, order = ib.placed[0]
    assert contract.symbol == "NRXS"
    assert order.action == "BUY"
    assert order.totalQuantity == 850.0


def test_flatten_position_noop_on_zero_qty():
    ib = FakeIB([])

    panic.flatten_position(ib, make_position("FLAT", 0.0))

    assert ib.placed == []


# -- 2026-09-21 regressions --------------------------------------------------

from datetime import datetime, timezone  # noqa: E402

PREMARKET = datetime(2026, 9, 21, 8, 18, tzinfo=timezone.utc)  # 04:18 ET, Monday
REGULAR = datetime(2026, 9, 21, 14, 0, tzinfo=timezone.utc)  # 10:00 ET, Monday


def _portfolio_item(symbol, price):
    return SimpleNamespace(contract=SimpleNamespace(symbol=symbol), marketPrice=price)


def _open_flatten(symbol, remaining):
    return SimpleNamespace(
        contract=SimpleNamespace(symbol=symbol),
        order=SimpleNamespace(orderRef=panic.FLATTEN_ORDER_REF, totalQuantity=remaining),
        orderStatus=SimpleNamespace(remaining=remaining),
    )


def test_premarket_flatten_is_a_marketable_limit_not_a_market_order(monkeypatch):
    monkeypatch.setattr(panic, "alert", lambda *a, **k: None)
    ib = FakeIB([], portfolio=[_portfolio_item("GTEC", 1.00)])

    placed = panic.flatten_position(ib, make_position("GTEC", 2546.0), limit_offset_pct=5.0, now=PREMARKET)

    assert placed
    _, order = ib.placed[0]
    assert order.orderType == "LMT"
    assert order.action == "SELL"
    assert order.lmtPrice == 0.95  # 5% through the last price
    assert order.outsideRth is True
    assert order.orderRef == panic.FLATTEN_ORDER_REF


def test_premarket_buy_to_cover_limit_sits_above_last_price(monkeypatch):
    monkeypatch.setattr(panic, "alert", lambda *a, **k: None)
    ib = FakeIB([], portfolio=[_portfolio_item("SHRT", 2.00)])

    panic.flatten_position(ib, make_position("SHRT", -100.0), limit_offset_pct=5.0, now=PREMARKET)

    _, order = ib.placed[0]
    assert order.action == "BUY"
    assert order.lmtPrice == 2.1


def test_regular_hours_flatten_is_a_market_order(monkeypatch):
    monkeypatch.setattr(panic, "alert", lambda *a, **k: None)
    ib = FakeIB([], portfolio=[_portfolio_item("GTEC", 1.00)])

    panic.flatten_position(ib, make_position("GTEC", 100.0), now=REGULAR)

    _, order = ib.placed[0]
    assert order.orderType == "MKT"


def test_premarket_without_a_price_falls_back_to_market(monkeypatch):
    monkeypatch.setattr(panic, "alert", lambda *a, **k: None)
    ib = FakeIB([], portfolio=[])

    panic.flatten_position(ib, make_position("GTEC", 100.0), now=PREMARKET)

    _, order = ib.placed[0]
    assert order.orderType == "MKT"


def test_flatten_not_resent_while_one_is_already_working(monkeypatch):
    # The reconciliation watchdog re-checks every 30s; each pass used to queue
    # another sell, and all of them filled at the open -> naked short.
    monkeypatch.setattr(panic, "alert", lambda *a, **k: None)
    ib = FakeIB([], open_trades=[_open_flatten("GTEC", 2546.0)], portfolio=[_portfolio_item("GTEC", 1.0)])

    placed = panic.flatten_position(ib, make_position("GTEC", 2546.0), now=PREMARKET)

    assert placed is False
    assert ib.placed == []


def test_partially_covered_position_gets_flattened_again(monkeypatch):
    monkeypatch.setattr(panic, "alert", lambda *a, **k: None)
    ib = FakeIB([], open_trades=[_open_flatten("GTEC", 1000.0)], portfolio=[_portfolio_item("GTEC", 1.0)])

    assert panic.flatten_position(ib, make_position("GTEC", 2546.0), now=PREMARKET)


def test_a_resting_stop_is_not_mistaken_for_a_working_flatten(monkeypatch):
    monkeypatch.setattr(panic, "alert", lambda *a, **k: None)
    stop = SimpleNamespace(
        contract=SimpleNamespace(symbol="GTEC"),
        order=SimpleNamespace(orderRef="", totalQuantity=2546.0),
        orderStatus=SimpleNamespace(remaining=2546.0),
    )
    ib = FakeIB([], open_trades=[stop], portfolio=[_portfolio_item("GTEC", 1.0)])

    assert panic.flatten_position(ib, make_position("GTEC", 2546.0), now=PREMARKET)


def test_panic_flatten_replaces_orders_that_reqglobalcancel_just_killed(monkeypatch):
    # openTrades() still lists the just-cancelled flatten order; panic_stop
    # must not treat it as still working.
    monkeypatch.setattr(panic, "alert", lambda *a, **k: None)
    ib = FakeIB(
        [make_position("GTEC", 2546.0)],
        open_trades=[_open_flatten("GTEC", 2546.0)],
        portfolio=[_portfolio_item("GTEC", 1.0)],
    )

    panic.flatten_all_positions(ib)

    assert len(ib.placed) == 1


# -- on_order_placed: regression coverage for the 2026-09-23 finding that
# emergency/EOD flatten fills were never journaled at all (ib.placeOrder()
# called with no listener attached), so a position force-closed by the
# reconciliation watchdog or a routine EOD flatten showed up in
# dashboard_report.py as "still open, $0 realized" forever.


def test_flatten_position_calls_on_order_placed_hook(monkeypatch):
    monkeypatch.setattr(panic, "alert", lambda *a, **k: None)
    ib = FakeIB([])
    calls = []

    panic.flatten_position(
        ib, make_position("GTEC", 500.0), on_order_placed=lambda symbol, trade, order: calls.append((symbol, order))
    )

    assert len(calls) == 1
    symbol, order = calls[0]
    assert symbol == "GTEC"
    assert order.totalQuantity == 500.0


def test_flatten_position_does_not_call_hook_when_nothing_placed(monkeypatch):
    monkeypatch.setattr(panic, "alert", lambda *a, **k: None)
    ib = FakeIB([])
    calls = []

    panic.flatten_position(
        ib, make_position("FLAT", 0.0), on_order_placed=lambda symbol, trade, order: calls.append(symbol)
    )

    assert calls == []


def test_flatten_position_does_not_call_hook_when_flatten_already_pending(monkeypatch):
    monkeypatch.setattr(panic, "alert", lambda *a, **k: None)
    ib = FakeIB([], open_trades=[_open_flatten("GTEC", 500.0)])
    calls = []

    panic.flatten_position(
        ib, make_position("GTEC", 500.0), on_order_placed=lambda symbol, trade, order: calls.append(symbol)
    )

    assert calls == []


def test_flatten_all_positions_threads_hook_to_every_position(monkeypatch):
    monkeypatch.setattr(panic, "alert", lambda *a, **k: None)
    ib = FakeIB([make_position("AAA", 100.0), make_position("BBB", 200.0)])
    calls = []

    panic.flatten_all_positions(ib, on_order_placed=lambda symbol, trade, order: calls.append(symbol))

    assert calls == ["AAA", "BBB"]


def test_panic_stop_threads_hook_through_to_flatten(monkeypatch):
    monkeypatch.setattr(panic, "alert", lambda *a, **k: None)
    ib = FakeIB([make_position("AAA", 100.0)])
    calls = []

    panic.panic_stop(ib, flatten=True, on_order_placed=lambda symbol, trade, order: calls.append(symbol))

    assert calls == ["AAA"]
