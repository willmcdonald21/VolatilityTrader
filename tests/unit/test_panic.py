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
        self.cancelled = []

    def openTrades(self):
        return self._open_trades

    # Mirrors ib_async: a blank account means every account.
    def portfolio(self, account=""):
        if account:
            return [p for p in self._portfolio if getattr(p, "account", "") == account]
        return self._portfolio

    def positions(self, account=""):
        if account:
            return [p for p in self._positions if getattr(p, "account", "") == account]
        return self._positions

    def placeOrder(self, contract, order):
        self.placed.append((contract, order))

    def cancelOrder(self, order):
        self.cancelled.append(order)
        # IBKR drops it from openTrades once the cancel is confirmed.
        self._open_trades = [t for t in self._open_trades if t.order is not order]


def make_position(symbol: str, qty: float, exchange: str = "NASDAQ", account=""):
    return SimpleNamespace(contract=FakeContract(symbol, exchange), position=qty, account=account)


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


# -- 2026-09-28 audit: stale flattens and the async global-cancel race --


def _flatten_trade(symbol="STUCK", qty=100.0, age_seconds=None, filled=0.0, cancelled=None):
    """A resting flatten order this module placed, optionally aged."""
    from datetime import datetime, timedelta, timezone

    log = []
    if age_seconds is not None:
        log = [SimpleNamespace(time=datetime.now(timezone.utc) - timedelta(seconds=age_seconds))]
    order = SimpleNamespace(
        orderRef=panic.FLATTEN_ORDER_REF, totalQuantity=qty, action="SELL", orderId=99, lmtPrice=1.0
    )
    return SimpleNamespace(
        contract=FakeContract(symbol),
        order=order,
        orderStatus=SimpleNamespace(remaining=qty, filled=filled, status="Submitted"),
        log=log,
    )


class CancellingFakeIB(FakeIB):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cancelled = []

    def cancelOrder(self, order):
        self.cancelled.append(order)


def test_fresh_pending_flatten_still_suppresses_a_duplicate(monkeypatch):
    monkeypatch.setattr(panic, "alert", lambda *a, **k: None)
    ib = CancellingFakeIB([make_position("STUCK", 100.0)], open_trades=[_flatten_trade(age_seconds=5)])

    placed = panic.flatten_position(ib, make_position("STUCK", 100.0))

    assert placed is False  # idempotency preserved
    assert ib.cancelled == []


def test_stale_unfilled_flatten_is_cancelled_and_reissued(monkeypatch):
    # A marketable limit priced off a stale portfolio marketPrice can rest
    # unfilled forever. Every later watchdog pass then saw the position as
    # "already covered" and returned before main.py's error log and alert,
    # so an unprotected position could sit behind a dead order all session.
    alerts = []
    monkeypatch.setattr(panic, "alert", lambda message, channel=None: alerts.append(message))
    stale = _flatten_trade(age_seconds=120)
    ib = CancellingFakeIB([make_position("STUCK", 100.0)], open_trades=[stale])

    placed = panic.flatten_position(ib, make_position("STUCK", 100.0))

    assert placed is True  # re-issued rather than silently trusted
    assert ib.cancelled == [stale.order]
    assert any("stalled unfilled" in message for message in alerts)


def test_stale_flatten_that_is_partially_filling_is_left_alone(monkeypatch):
    monkeypatch.setattr(panic, "alert", lambda *a, **k: None)
    working = _flatten_trade(age_seconds=120, filled=40.0)
    ib = CancellingFakeIB([make_position("STUCK", 100.0)], open_trades=[working])

    placed = panic.flatten_position(ib, make_position("STUCK", 100.0))

    assert placed is False  # it IS filling, just slowly
    assert ib.cancelled == []


class SlowCancelIB(FakeIB):
    """openTrades() stays non-empty for the first `clears_after` polls,
    mimicking reqGlobalCancel's asynchronous behaviour at IBKR.
    `clears_after=None` never clears."""

    def __init__(self, positions, clears_after=2, **kwargs):
        super().__init__(positions, **kwargs)
        self._polls = 0
        self._clears_after = clears_after
        self.slept = 0

    def openTrades(self):
        if self._clears_after is None:
            return [_flatten_trade()]
        return [] if self._polls >= self._clears_after else [_flatten_trade()]

    def sleep(self, seconds):
        self._polls += 1
        self.slept += 1


def test_panic_stop_waits_for_the_global_cancel_before_flattening(monkeypatch):
    # reqGlobalCancel is asynchronous; flattening straight after it can put
    # a sell on the tape while a protective stop is still live.
    monkeypatch.setattr(panic, "alert", lambda *a, **k: None)
    ib = SlowCancelIB([make_position("AAA", 100.0)], clears_after=2)

    panic.panic_stop(ib)

    assert ib.slept >= 2  # actually waited
    assert len(ib.placed) == 1  # and still flattened afterwards


def test_panic_stop_flattens_anyway_and_alerts_if_the_cancel_never_clears(monkeypatch):
    alerts = []
    monkeypatch.setattr(panic, "alert", lambda message, channel=None: alerts.append(message))
    monkeypatch.setattr(panic, "_GLOBAL_CANCEL_TIMEOUT_SECONDS", 0.2)
    ib = SlowCancelIB([make_position("AAA", 100.0)], clears_after=None)  # never clears

    panic.panic_stop(ib)

    assert any("flattening anyway" in message for message in alerts)
    assert len(ib.placed) == 1  # getting flat still wins


# --- account scoping ------------------------------------------------------
#
# The panic path used to call reqGlobalCancel(), which takes no account
# argument and cancels everything the login can see. With a second account
# linked under the same username that reaches another bot's working orders --
# and that bot manages its stops synthetically, so cancelling its entry limits
# is a real intervention in a strategy this one knows nothing about.


def _order_in(account: str, symbol="UCAR", order_type="STP LMT"):
    """A working order belonging to `account` -- a protective stop, not a
    flatten, so it is the kind the old reqGlobalCancel would have swept up."""
    order = SimpleNamespace(
        orderRef="", totalQuantity=100.0, action="SELL", orderType=order_type,
        orderId=1, lmtPrice=1.0, account=account,
    )
    return SimpleNamespace(
        contract=FakeContract(symbol),
        order=order,
        orderStatus=SimpleNamespace(remaining=100.0, filled=0.0, status="Submitted"),
        log=[],
    )


def test_cancel_all_orders_cancels_only_our_account(monkeypatch):
    monkeypatch.setattr(panic, "alert", lambda *a, **k: None)
    mine = _order_in("DU111")
    theirs = _order_in("DU999", symbol="SPX", order_type="LMT")
    ib = FakeIB([], open_trades=[mine, theirs])

    panic.cancel_all_orders(ib, account="DU111")

    assert ib.cancelled == [mine.order]


def test_cancel_all_orders_reports_what_it_left_alone(monkeypatch):
    messages = []
    monkeypatch.setattr(panic, "alert", lambda msg, **k: messages.append(msg))
    ib = FakeIB([], open_trades=[_order_in("DU111"), _order_in("DU999", symbol="SPX")])

    panic.cancel_all_orders(ib, account="DU111")

    assert "1 order(s) in other accounts left alone" in messages[0]


def test_with_no_account_configured_everything_visible_is_ours(monkeypatch):
    """Today's behaviour: one account under the login, so nothing to exclude."""
    monkeypatch.setattr(panic, "alert", lambda *a, **k: None)
    a, b = _order_in(""), _order_in("", symbol="BIRD")
    ib = FakeIB([], open_trades=[a, b])

    panic.cancel_all_orders(ib)

    assert ib.cancelled == [a.order, b.order]


def test_an_order_with_no_account_is_treated_as_ours(monkeypatch):
    """IBKR leaves the field blank on a single-account login, so a blank order
    in a configured-account world is still one of ours."""
    monkeypatch.setattr(panic, "alert", lambda *a, **k: None)
    blank = _order_in("")
    ib = FakeIB([], open_trades=[blank])

    panic.cancel_all_orders(ib, account="DU111")

    assert ib.cancelled == [blank.order]


def test_waiting_for_cancels_ignores_another_accounts_orders(monkeypatch):
    """Otherwise a foreign working order holds up every panic until timeout."""
    monkeypatch.setattr(panic, "alert", lambda *a, **k: None)
    ib = FakeIB([], open_trades=[_order_in("DU999", symbol="SPX")])

    assert panic._await_global_cancel(ib, timeout=0.05, account="DU111") is True


def test_flatten_only_touches_our_accounts_positions(monkeypatch):
    monkeypatch.setattr(panic, "alert", lambda *a, **k: None)
    ib = FakeIB([
        make_position("UCAR", 770.0, account="DU111"),
        make_position("SPX", 5.0, account="DU999"),
    ])

    panic.flatten_all_positions(ib, account="DU111")

    assert [c.symbol for c, _ in ib.placed] == ["UCAR"]


def test_a_flatten_order_names_the_account_it_is_closing(monkeypatch):
    """Mandatory once the login manages more than one account, and taken from
    the position itself so a flatten targets where the shares actually are."""
    monkeypatch.setattr(panic, "alert", lambda *a, **k: None)
    ib = FakeIB([make_position("UCAR", 770.0, account="DU111")])

    panic.flatten_all_positions(ib, account="DU111")

    (_, order) = ib.placed[0]
    assert order.account == "DU111"


def test_a_flatten_falls_back_to_the_configured_account(monkeypatch):
    """main.py's reconciliation calls flatten_position directly with a position
    it already holds. If that position carries no account, the order still has
    to name one or IBKR rejects it."""
    monkeypatch.setattr(panic, "alert", lambda *a, **k: None)
    position = SimpleNamespace(contract=FakeContract("UCAR", "NASDAQ"), position=770.0)
    ib = FakeIB([position])

    assert panic.flatten_position(ib, position, account="DU111") is True

    (_, order) = ib.placed[0]
    assert order.account == "DU111"


def test_panic_stop_scopes_both_halves(monkeypatch):
    monkeypatch.setattr(panic, "alert", lambda *a, **k: None)
    mine = _order_in("DU111")
    theirs = _order_in("DU999", symbol="SPX")
    ib = FakeIB(
        [make_position("UCAR", 770.0, account="DU111"), make_position("SPX", 5.0, account="DU999")],
        open_trades=[mine, theirs],
    )

    panic.panic_stop(ib, account="DU111")

    assert ib.cancelled == [mine.order]
    assert [c.symbol for c, _ in ib.placed] == ["UCAR"]


def test_reqGlobalCancel_is_never_called(monkeypatch):
    """It takes no account argument, so it cannot be scoped and must not come
    back. Asserted by making the call itself fail rather than by grepping the
    source, which would also match the comment explaining the history."""
    monkeypatch.setattr(panic, "alert", lambda *a, **k: None)

    class Tripwire(FakeIB):
        def reqGlobalCancel(self):
            raise AssertionError("reqGlobalCancel cannot be scoped to an account")

    ib = Tripwire([make_position("UCAR", 770.0, account="DU111")], open_trades=[_order_in("DU111")])
    panic.panic_stop(ib, account="DU111")

    assert ib.cancelled
