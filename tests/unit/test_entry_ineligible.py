"""Symbols IBKR will not let this account OPEN.

DKI, 2026-10-08: it gapped 1.67 -> 5.48 overnight and took the top scanner
rank, so bull_flag signalled it at 06:54:05 and the bracket went out. IBKR
rejected all three legs with code 201 -- the product is in closing-only
status, meaning existing positions may be closed but new ones may not be
opened. The bot had no concept of that, so it treated the rejected entry
like one that simply had not filled yet: the lot held a
max_concurrent_positions slot and the cross-strategy gate (the concurrent
abcd signal on DKI was turned away) until the 300s entry-fill timeout
released it at 06:59:15.

Nothing was lost -- but nothing was learned either, and the restriction is
a regulatory state on the product lasting weeks, not a fact about that
order. 94 "No Trading Permission" rejections landed between 2026-08-27 and
09-24, 72 of them naming closing-only status; the scanner selects for
exactly the names it happens to.

Each test below names the production behaviour it pins.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.unit.test_risk_manager import default_snapshot, make_risk_manager, make_signal
from warrior_bot.persistence.db import get_connection
from warrior_bot.persistence.journal import Journal

# The real text IBKR sent for DKI, <br> tags and all.
DKI_REJECTION = (
    "Order rejected - reason:No Trading Permission, Customer Ineligible; "
    "Ineligibility reasons:<br>Other regulatory requirements: Due to regulatory "
    "requirements, this product is in closing-only status. You may<br> close existing "
    "positions but not open new ones."
)

# Also code 201, but transient -- a smaller size or a later attempt can work.
MARGIN_REJECTION = (
    "Order rejected - reason:We are unable to accept your order. Your Available Funds "
    "are in sufficient to cover the change in the account's margin requirements if this "
    "order executes."
)

# Also code 201, also transient: this bot cancels and replaces constantly.
WORKING_ORDERS_REJECTION = (
    "Order rejected - reason:Your account has a minimum of 15 orders working on either "
    "the buy or sell side for this particular contract."
)


# --------------------------------------------------------------------------
# Journal: remembering the restriction across restarts
# --------------------------------------------------------------------------


def test_load_entry_ineligible_empty_when_nothing_recorded(tmp_path):
    journal = Journal(get_connection(tmp_path / "journal.sqlite3"))

    assert journal.load_entry_ineligible() == {}


def test_record_and_load_entry_ineligible_round_trips(tmp_path):
    journal = Journal(get_connection(tmp_path / "journal.sqlite3"))

    journal.record_entry_ineligible("DKI", 201, "closing-only status")

    assert journal.load_entry_ineligible() == {"DKI": "closing-only status"}


def test_repeat_rejections_count_up_without_duplicating(tmp_path):
    """The row is the symbol, so a second rejection updates rather than
    inserts -- and first_seen keeps pointing at when it started."""
    journal = Journal(get_connection(tmp_path / "journal.sqlite3"))

    journal.record_entry_ineligible("DKI", 201, "closing-only status")
    first = journal.conn.execute(
        "SELECT first_seen FROM entry_ineligible_symbols WHERE symbol='DKI'"
    ).fetchone()[0]
    journal.record_entry_ineligible("DKI", 201, "closing-only status")

    row = journal.conn.execute(
        "SELECT rejections, first_seen FROM entry_ineligible_symbols WHERE symbol='DKI'"
    ).fetchone()
    assert row[0] == 2
    assert row[1] == first
    count = journal.conn.execute("SELECT COUNT(*) FROM entry_ineligible_symbols").fetchone()[0]
    assert count == 1


def test_entry_ineligibility_is_not_scoped_to_a_date(tmp_path):
    """Unlike symbol_loss_state, this must survive the day rollover -- the
    restriction is on the product, not the session."""
    journal = Journal(get_connection(tmp_path / "journal.sqlite3"))
    journal.record_entry_ineligible("DKI", 201, "closing-only status")

    reopened = Journal(get_connection(tmp_path / "journal.sqlite3"))

    assert reopened.load_entry_ineligible() == {"DKI": "closing-only status"}


# --------------------------------------------------------------------------
# RiskManager: the gate
# --------------------------------------------------------------------------


def test_signal_rejected_for_an_ineligible_symbol(tmp_path):
    rm = make_risk_manager(tmp_path, default_snapshot())
    rm.mark_entry_ineligible("TEST", "closing-only status")

    decision = rm.evaluate(make_signal())

    assert not decision.accepted
    assert "entry_ineligible" in decision.reason


def test_gate_applies_across_strategies(tmp_path):
    """IBKR refuses the product, not the idea -- so a different strategy
    arriving at the same symbol is refused on the same grounds."""
    rm = make_risk_manager(tmp_path, default_snapshot())
    rm.mark_entry_ineligible("TEST", "closing-only status")
    signal = make_signal()
    signal.strategy = "vwap_reversion"

    assert not rm.evaluate(signal).accepted


def test_other_symbols_are_unaffected(tmp_path):
    rm = make_risk_manager(tmp_path, default_snapshot())
    rm.mark_entry_ineligible("DKI", "closing-only status")

    assert rm.evaluate(make_signal()).accepted  # make_signal() is TEST


def test_restore_replaces_the_set(tmp_path):
    rm = make_risk_manager(tmp_path, default_snapshot())
    rm.mark_entry_ineligible("TEST", "closing-only status")

    rm.restore_entry_ineligible({"DKI": "closing-only status"})

    assert rm.evaluate(make_signal()).accepted  # TEST no longer banned


def test_gate_precedes_capacity_so_the_reason_is_the_real_one(tmp_path):
    """Checked before max_concurrent_positions: an unopenable symbol should
    report why it is unopenable, not that the book happened to be full."""
    rm = make_risk_manager(tmp_path, default_snapshot(), open_lots=0, max_concurrent_positions=1)
    rm.mark_entry_ineligible("TEST", "closing-only status")

    assert "entry_ineligible" in rm.evaluate(make_signal()).reason


# --------------------------------------------------------------------------
# OrderManager: learning it from IBKR's own refusal
# --------------------------------------------------------------------------


class RecordingJournal:
    def __init__(self):
        self.ineligible = []

    def record_entry_ineligible(self, symbol, error_code, reason):
        self.ineligible.append((symbol, error_code, reason))


class RecordingPositionManager:
    def __init__(self):
        self.released = []

    def release_rejected_entry(self, symbol):
        self.released.append(symbol)
        return 1


def make_om(journal=None, position_manager=None):
    from warrior_bot.execution.order_manager import OrderManager

    om = OrderManager(
        ib=None,
        journal=journal or RecordingJournal(),
        exits_config=None,
        position_manager=position_manager or RecordingPositionManager(),
    )
    om._order_symbols[60730] = "DKI"
    return om


def test_permission_rejection_bans_the_symbol():
    journal = RecordingJournal()
    pm = RecordingPositionManager()
    om = make_om(journal, pm)
    seen = []
    om.on_entry_ineligible = lambda s, r: seen.append((s, r))

    om._on_order_error(60730, 201, DKI_REJECTION, None)

    assert [row[0] for row in journal.ineligible] == ["DKI"]
    assert journal.ineligible[0][1] == 201
    assert [s for s, _ in seen] == ["DKI"]
    assert pm.released == ["DKI"]


def test_the_rejection_reason_is_flattened_for_logging():
    """IBKR embeds <br> tags; they should not reach the journal or Discord."""
    journal = RecordingJournal()
    om = make_om(journal)

    om._on_order_error(60730, 201, DKI_REJECTION, None)

    reason = journal.ineligible[0][2]
    assert "<br>" not in reason
    assert "closing-only status" in reason


@pytest.mark.parametrize("reason", [MARGIN_REJECTION, WORKING_ORDERS_REJECTION])
def test_transient_201s_do_not_ban_the_symbol(reason):
    """Code 201 covers unrelated refusals. Margin shortfalls and the
    15-working-orders cap can both succeed on a later or smaller attempt --
    1,006 of the latter landed in 2026-08/09, and banning on them would
    have retired most of the universe."""
    journal = RecordingJournal()
    pm = RecordingPositionManager()
    om = make_om(journal, pm)

    om._on_order_error(60730, 201, reason, None)

    assert journal.ineligible == []
    assert pm.released == []


def test_unrelated_error_codes_are_ignored():
    journal = RecordingJournal()
    om = make_om(journal)

    om._on_order_error(60730, 202, DKI_REJECTION, None)  # 202 = routine cancel

    assert journal.ineligible == []


def test_an_unknown_order_id_is_ignored():
    """errorEvent is account-wide and arrives with contract=None, so an
    order this process never placed must not be attributed to a symbol."""
    journal = RecordingJournal()
    om = make_om(journal)

    om._on_order_error(999999, 201, DKI_REJECTION, None)

    assert journal.ineligible == []


def test_the_ban_survives_a_failing_journal():
    """Runs inside an eventkit handler. A persist failure must still leave
    the in-process ban and the lot release in effect."""

    class ExplodingJournal(RecordingJournal):
        def record_entry_ineligible(self, *a, **k):
            raise RuntimeError("disk full")

    pm = RecordingPositionManager()
    om = make_om(ExplodingJournal(), pm)
    seen = []
    om.on_entry_ineligible = lambda s, r: seen.append(s)

    om._on_order_error(60730, 201, DKI_REJECTION, None)

    assert seen == ["DKI"]
    assert pm.released == ["DKI"]


def test_a_failing_release_still_bans_the_symbol():
    class ExplodingPM:
        def release_rejected_entry(self, symbol):
            raise RuntimeError("boom")

    journal = RecordingJournal()
    om = make_om(journal, ExplodingPM())

    om._on_order_error(60730, 201, DKI_REJECTION, None)

    assert [row[0] for row in journal.ineligible] == ["DKI"]


# --------------------------------------------------------------------------
# PositionManager: releasing the lot without waiting 300s
# --------------------------------------------------------------------------


def test_release_rejected_entry_drops_an_unfilled_lot():
    from tests.unit.test_position_manager import (
        FakeIB,
        FakeJournal,
        FakeOrder,
        FakeTrade,
        make_exits_config,
        make_signal as make_pm_signal,
    )
    from warrior_bot.execution.position_manager import PositionManager

    pm = PositionManager(FakeIB(), FakeJournal(), make_exits_config(trailing_enabled=False))
    signal = make_pm_signal(entry=10.0, stop=9.0)
    pm.track(
        contract=SimpleNamespace(symbol=signal.symbol),
        signal=signal,
        signal_id=1,
        parent_trade=FakeTrade(FakeOrder("BUY", 100, lmtPrice=10.0, orderId=1)),
        stop_trade=FakeTrade(FakeOrder("SELL", 100, auxPrice=9.0, orderId=2, parentId=1)),
        stop_row_id=1,
        target_trades=[FakeTrade(FakeOrder("SELL", 100, lmtPrice=12.0, orderId=3))],
        target_roles=["target"],
    )
    assert pm.open_lot_count("TEST") == 1

    released = pm.release_rejected_entry("TEST")

    assert released == 1
    assert pm.open_lot_count("TEST") == 0
    assert not pm.has_unfilled_entry("TEST")


def test_release_rejected_entry_keeps_a_partially_filled_lot():
    """One leg of a bracket being rejected does not void shares that are
    already held -- those are a real position and keep their stop."""
    from tests.unit.test_position_manager import (
        FakeIB,
        FakeJournal,
        FakeOrder,
        FakeTrade,
        flush_resize,
        make_exits_config,
        make_fill,
        make_signal as make_pm_signal,
    )
    from warrior_bot.execution.position_manager import PositionManager

    pm = PositionManager(FakeIB(), FakeJournal(), make_exits_config(trailing_enabled=False))
    signal = make_pm_signal(entry=10.0, stop=9.0)
    parent = FakeTrade(FakeOrder("BUY", 100, lmtPrice=10.0, orderId=1))
    pm.track(
        contract=SimpleNamespace(symbol=signal.symbol),
        signal=signal,
        signal_id=1,
        parent_trade=parent,
        stop_trade=FakeTrade(FakeOrder("SELL", 100, auxPrice=9.0, orderId=2, parentId=1)),
        stop_row_id=1,
        target_trades=[FakeTrade(FakeOrder("SELL", 100, lmtPrice=12.0, orderId=3))],
        target_roles=["target"],
    )
    parent.fillEvent.emit(parent, make_fill(100, price=10.0))
    flush_resize(pm._positions["TEST"][0])

    released = pm.release_rejected_entry("TEST")

    assert released == 0
    assert pm.open_lot_count("TEST") == 1


def test_release_rejected_entry_is_a_noop_for_an_untracked_symbol():
    from tests.unit.test_position_manager import FakeIB, FakeJournal, make_exits_config
    from warrior_bot.execution.position_manager import PositionManager

    pm = PositionManager(FakeIB(), FakeJournal(), make_exits_config(trailing_enabled=False))

    assert pm.release_rejected_entry("NOPE") == 0
