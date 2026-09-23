from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from ib_async.util import UNSET_DOUBLE

from warrior_bot.risk.account_state import AccountState


def make_fill(symbol, side, shares, price, commission=0.0, minutes_ago=0, realized_pnl=0.0):
    """`realized_pnl` defaults to 0.0 to mirror what IBKR's paper simulator
    actually reports on closing fills -- the whole reason daily_realized_pnl
    can't be built out of that field."""
    return SimpleNamespace(
        time=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
        contract=SimpleNamespace(symbol=symbol),
        execution=SimpleNamespace(side=side, shares=shares, price=price),
        commissionReport=SimpleNamespace(commission=commission, realizedPNL=realized_pnl),
    )


class FakeIB:
    def __init__(self, fills, portfolio=None, positions=None):
        self._fills = fills
        self._portfolio = portfolio or []
        self._positions = positions or []

    def portfolio(self, account=""):
        return self._portfolio

    def fills(self):
        return self._fills

    def positions(self, account=""):
        return self._positions

    def accountValues(self, account=""):
        return []


def make_state(fills, session_started_minutes_ago=60) -> AccountState:
    """AccountState stamps its session start at construction, so tests that
    place fills in the past have to move it back to cover them."""
    state = AccountState(FakeIB(fills))
    state._session_start = datetime.now(timezone.utc) - timedelta(minutes=session_started_minutes_ago)
    return state


def test_realized_pnl_computed_from_fill_prices_not_broker_realized_pnl():
    # The exact situation that kept the daily loss limit disarmed: a real
    # losing round trip where IBKR reports realizedPNL=0 on the closing fill.
    state = make_state(
            [
                make_fill("ABCD", "BOT", 1000, 2.00, minutes_ago=10),
                make_fill("ABCD", "SLD", 1000, 1.90, minutes_ago=5),
            ]
    )

    assert state.daily_realized_pnl() == pytest.approx(-100.0)


def test_realized_pnl_nets_commissions():
    state = make_state(
            [
                make_fill("ABCD", "BOT", 100, 5.00, commission=1.25, minutes_ago=10),
                make_fill("ABCD", "SLD", 100, 6.00, commission=1.75, minutes_ago=5),
            ]
    )

    assert state.daily_realized_pnl() == pytest.approx(100.0 - 3.0)


def test_realized_pnl_uses_average_cost_across_partial_entries():
    # Two entry lots at different prices, one exit -- the P&L has to price
    # the exit against the blended cost, not the first or last fill.
    state = make_state(
            [
                make_fill("ABCD", "BOT", 100, 1.00, minutes_ago=10),
                make_fill("ABCD", "BOT", 300, 2.00, minutes_ago=9),
                make_fill("ABCD", "SLD", 400, 2.00, minutes_ago=5),
            ]
    )

    # avg cost = (100*1 + 300*2)/400 = 1.75; exit at 2.00 on 400 shares
    assert state.daily_realized_pnl() == pytest.approx(100.0)


def test_realized_pnl_keeps_symbols_independent():
    state = make_state(
            [
                make_fill("AAA", "BOT", 100, 10.00, minutes_ago=10),
                make_fill("BBB", "BOT", 100, 1.00, minutes_ago=9),
                make_fill("BBB", "SLD", 100, 2.00, minutes_ago=8),
                make_fill("AAA", "SLD", 100, 9.00, minutes_ago=7),
            ]
    )

    assert state.daily_realized_pnl() == pytest.approx(100.0 - 100.0)


def test_realized_pnl_ignores_open_position_with_no_exit_yet():
    state = make_state([make_fill("ABCD", "BOT", 500, 3.00, minutes_ago=5)])

    assert state.daily_realized_pnl() == pytest.approx(0.0)


def test_realized_pnl_excludes_fills_from_before_this_session():
    state = make_state(
            [
                make_fill("ABCD", "BOT", 100, 1.00, minutes_ago=600),
                make_fill("ABCD", "SLD", 100, 5.00, minutes_ago=590),
            ]
    )

    assert state.daily_realized_pnl() == pytest.approx(0.0)


def test_realized_pnl_ignores_unset_double_commission_sentinel():
    state = make_state(
            [
                make_fill("ABCD", "BOT", 100, 1.00, commission=UNSET_DOUBLE, minutes_ago=10),
                make_fill("ABCD", "SLD", 100, 2.00, commission=UNSET_DOUBLE, minutes_ago=5),
            ]
    )

    assert state.daily_realized_pnl() == pytest.approx(100.0)


def test_realized_pnl_counts_only_the_matched_portion_of_an_oversized_sell():
    # The 2026-09-16 NRXS shape: a duplicate exit sold more than was ever
    # bought. The matched 100 shares are a real round trip; the extra 100
    # have no cost basis in this session and must not invent one.
    state = make_state(
            [
                make_fill("NRXS", "BOT", 100, 2.00, minutes_ago=10),
                make_fill("NRXS", "SLD", 200, 1.50, minutes_ago=5),
            ]
    )

    assert state.daily_realized_pnl() == pytest.approx(-50.0)


def _item(symbol, position, unrealized):
    return SimpleNamespace(contract=SimpleNamespace(symbol=symbol), position=position, unrealizedPNL=unrealized)


def test_unrealized_pnl_sums_open_positions_and_ignores_nan_and_flat():
    ib = FakeIB([], portfolio=[_item("A", 100, -250.0), _item("B", 50, 40.0), _item("C", 10, float("nan")), _item("D", 0, -999.0)])

    assert AccountState(ib).unrealized_pnl() == -210.0


def test_snapshot_exposes_open_symbols_and_unrealized():
    pos = SimpleNamespace(contract=SimpleNamespace(symbol="GTEC"), position=2546.0)
    flat = SimpleNamespace(contract=SimpleNamespace(symbol="OLD"), position=0.0)
    ib = FakeIB([], portfolio=[_item("GTEC", 2546, -300.0)], positions=[pos, flat])

    snap = AccountState(ib).snapshot()

    assert snap.open_symbols == frozenset({"GTEC"})
    assert snap.daily_unrealized_pnl == -300.0
    assert snap.open_positions_count == 1


# -- _session_start construction: regression coverage for the 2026-09-23
# finding that a mid-day restart silently reset daily_realized_pnl to ~$0
# and lifted the daily-loss-limit halt, because AccountState previously
# stamped _session_start at "now" (the moment the process happened to
# start) instead of the actual start of the ET trading day.


def test_fresh_account_state_anchors_session_start_to_today_not_now():
    from warrior_bot.utils.time_utils import session_date_start

    before_construction = session_date_start().astimezone(timezone.utc)
    state = AccountState(FakeIB([]))

    # Anchored to midnight ET today, not "now" -- must be exactly today's
    # ET day-start (allowing no drift at all, since both sides compute it
    # the same way), and nowhere near datetime.now(timezone.utc).
    assert state._session_start == before_construction
    assert (datetime.now(timezone.utc) - state._session_start) > timedelta(hours=1)


def test_reset_session_also_anchors_to_today_not_now():
    from warrior_bot.utils.time_utils import session_date_start

    state = AccountState(FakeIB([]))
    state._session_start = datetime.now(timezone.utc)  # simulate the old, buggy behavior

    state.reset_session()

    assert state._session_start == session_date_start().astimezone(timezone.utc)


def test_fresh_account_state_still_sees_a_fill_from_earlier_today():
    # The actual restart scenario: a real loss happened hours before the
    # process (re)started -- a brand-new AccountState, with no manual
    # _session_start override, must still count it.
    old_fill = make_fill("AAPL", "SLD", 100, 9.0, minutes_ago=180, realized_pnl=0.0)  # 3 hours ago
    entry_fill = make_fill("AAPL", "BOT", 100, 10.0, minutes_ago=185, realized_pnl=None)
    state = AccountState(FakeIB([entry_fill, old_fill]))

    assert state.daily_realized_pnl() == pytest.approx(-100.0)  # (9.0 - 10.0) * 100
