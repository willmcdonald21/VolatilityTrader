"""One loss per symbol per day (risk.max_losses_per_symbol_per_day).

`allow_cross_strategy_stacking` already blocks two strategies holding the
same name at once, but it inspects OPEN lots -- once the first strategy
has stopped out there is nothing left to conflict with, so the next
strategy walks straight back into the symbol that just took money off us.
Confirmed across the journal (2026-09-14 onward): 38 of 200 closed trades
were exactly this, winning 23.7% of the time for -$1,616.81, a third of
all losses. PFSA was entered three times on 09-24 (-$393.59); MASK twice
on 09-22 (-$393.42), each time by a different strategy.

Each test below names the production behaviour it pins.
"""

from __future__ import annotations

import pytest

import asyncio
from types import SimpleNamespace

from warrior_bot.execution import position_manager as position_manager_module
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
from tests.unit.test_risk_manager import default_snapshot, make_risk_manager, make_signal
from warrior_bot.execution.position_manager import PositionManager


@pytest.fixture(autouse=True)
def _fast_debounced_resize(monkeypatch):
    """Same setup test_position_manager.py uses, and needed here for the
    same reason: an entry fill schedules its stop resize on the event loop
    (see _schedule_stop_resize), and these tests fill entries. An autouse
    fixture does not travel with an imported helper, so without this the
    module passes alone and fails in the full suite, where no loop is set."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    monkeypatch.setattr(position_manager_module, "_STOP_RESIZE_DEBOUNCE_SECONDS", 0)
    yield
    pending = asyncio.all_tasks(loop)
    for task in pending:
        task.cancel()
    if pending:
        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
    loop.close()


# --------------------------------------------------------------------------
# PositionManager: which finished lots count as a loss
# --------------------------------------------------------------------------


def _track(quantity: int = 100, entry: float = 10.0):
    """Builds a tracked lot and hands back the parent and stop Trades.

    test_position_manager's own track_position helper is not reused here:
    it emits the entry fill with make_fill()'s default price of 0.0, which
    would leave the lot with no cost basis and make every outcome
    unscoreable -- exactly the thing these tests are about."""
    pm = PositionManager(FakeIB(), FakeJournal(), make_exits_config(trailing_enabled=False))
    signal = make_pm_signal(entry=entry, stop=9.0)
    parent_order = FakeOrder("BUY", quantity, lmtPrice=entry, orderId=1)
    parent_trade = FakeTrade(parent_order)
    stop_order = FakeOrder("SELL", quantity, auxPrice=9.0, orderId=2, parentId=1)
    stop_trade = FakeTrade(stop_order)
    target_trade = FakeTrade(FakeOrder("SELL", quantity, lmtPrice=12.0, orderId=3))
    pm.track(
        contract=SimpleNamespace(symbol=signal.symbol),
        signal=signal,
        signal_id=1,
        parent_trade=parent_trade,
        stop_trade=stop_trade,
        stop_row_id=1,
        target_trades=[target_trade],
        target_roles=["target"],
    )
    return pm, parent_trade, stop_trade


def _closed_lot(exit_price: float, quantity: int = 100, entry: float = 10.0) -> PositionManager:
    """Runs a lot all the way through: entry fills at `entry`, then the
    stop fills the whole position at `exit_price`, which is what takes
    _close_out down its parent_done branch."""
    pm, parent_trade, stop_trade = _track(quantity=quantity, entry=entry)
    parent_trade.fillEvent.emit(parent_trade, make_fill(quantity, price=entry))
    flush_resize(pm._positions["TEST"][0])

    # The entry fill cancel-and-replaces the stop, so the live order is a
    # fresh object placed through ib.placeOrder, not the one track() got.
    live_stop = pm._positions["TEST"][0].stop_order
    live_trade = next((t for t in pm.ib.trades if t.order is live_stop), stop_trade)
    live_trade.fillEvent.emit(live_trade, make_fill(quantity, price=exit_price))
    return pm


def test_lot_closing_net_negative_counts_as_a_loss():
    pm = _closed_lot(exit_price=9.0)  # bought 100 @ 10, sold 100 @ 9
    assert pm.losing_lots_today("TEST") == 1


def test_lot_closing_net_positive_does_not_count():
    """A trailing stop that fills in profit is the normal winning exit in
    this bot -- 192 of 205 journalled exits are stop fills, most of the
    winners among them. Scoring those as losses would burn the symbol
    after a win."""
    pm = _closed_lot(exit_price=11.0)
    assert pm.losing_lots_today("TEST") == 0


def test_exact_breakeven_scratch_does_not_count():
    """A breakeven stop fills at the entry price by construction. The data
    says a scratch threshold is unnecessary (counting only losses worse
    than 0.10R or 0.25R selects the identical 38 trades), but an exact
    scratch must not tip into the loss bucket on float noise."""
    pm = _closed_lot(exit_price=10.0)
    assert pm.losing_lots_today("TEST") == 0


def test_flat_for_now_while_parent_still_filling_is_not_scored():
    """The BLSG 2026-09-14 case: a fast tier fill sells everything bought
    SO FAR while the parent entry is still working. _close_out keeps the
    lot tracked, and it is not a finished trade, so it must not be scored
    -- doing so would lock out a symbol mid-entry."""
    pm, parent_trade, stop_trade = _track(quantity=100, entry=10.0)
    # 40 of 100 shares in, parent still working (orderStatus.remaining>0
    # is what on_entry_fill reads to set parent_done).
    parent_trade.orderStatus.remaining = 60
    parent_trade.fillEvent.emit(parent_trade, make_fill(40, price=10.0))
    flush_resize(pm._positions["TEST"][0])

    live_stop = pm._positions["TEST"][0].stop_order
    live_trade = next((t for t in pm.ib.trades if t.order is live_stop), stop_trade)
    live_trade.fillEvent.emit(live_trade, make_fill(40, price=9.0))

    assert pm.losing_lots_today("TEST") == 0
    assert "TEST" in pm._positions  # still tracked, per _close_out


def test_lot_that_never_filled_is_not_a_loss():
    """A cancelled/unfilled entry has no cost basis. Counting it would
    burn a symbol the bot never actually traded -- 103 of 377 accepted
    signals in the journal never filled."""
    pm, _parent, _stop = _track(quantity=100, entry=10.0)
    pos = pm._positions["TEST"][0]
    pos.parent_done = True

    pm._record_lot_outcome(pos)

    assert pm.losing_lots_today("TEST") == 0


def test_reset_daily_losses_clears_the_counter():
    pm = _closed_lot(exit_price=9.0)
    assert pm.losing_lots_today("TEST") == 1

    pm.reset_daily_losses()

    assert pm.losing_lots_today("TEST") == 0


def test_clear_does_not_forget_the_days_losses():
    """clear() also runs mid-day from _trigger_flatten (main.py). Resetting
    the loss record there would re-open every symbol that had already
    burnt us, which is the opposite of what a flatten means."""
    pm = _closed_lot(exit_price=9.0)

    pm.clear()

    assert pm.losing_lots_today("TEST") == 1


# --------------------------------------------------------------------------
# RiskManager: the gate
# --------------------------------------------------------------------------


def test_signal_rejected_after_the_symbol_already_lost_today(tmp_path):
    rm = make_risk_manager(
        tmp_path, default_snapshot(), max_losses_per_symbol_per_day=1, losing_lots=1
    )

    decision = rm.evaluate(make_signal())

    assert not decision.accepted
    assert "symbol_loss_cap" in decision.reason


def test_gate_applies_across_strategies(tmp_path):
    """The whole point: gap_and_go stops out, then vwap_reversion re-enters
    the same name an hour later. The gate is keyed on the symbol, not on
    which strategy lost."""
    rm = make_risk_manager(
        tmp_path, default_snapshot(), max_losses_per_symbol_per_day=1, losing_lots=1
    )
    signal = make_signal()
    signal.strategy = "vwap_reversion"

    decision = rm.evaluate(signal)

    assert not decision.accepted
    assert "symbol_loss_cap" in decision.reason


def test_symbol_with_no_prior_loss_is_unaffected(tmp_path):
    rm = make_risk_manager(
        tmp_path, default_snapshot(), max_losses_per_symbol_per_day=1, losing_lots=0
    )

    assert rm.evaluate(make_signal()).accepted


def test_zero_disables_the_gate(tmp_path):
    rm = make_risk_manager(
        tmp_path, default_snapshot(), max_losses_per_symbol_per_day=0, losing_lots=5
    )

    assert rm.evaluate(make_signal()).accepted


def test_cap_above_one_allows_a_second_attempt(tmp_path):
    rm = make_risk_manager(
        tmp_path, default_snapshot(), max_losses_per_symbol_per_day=2, losing_lots=1
    )

    assert rm.evaluate(make_signal()).accepted


def test_pyramid_addon_still_allowed_when_nothing_has_closed_red(tmp_path):
    """The gate must not disarm the intentional add-on path: one open lot,
    same strategy, no closed loss -- still sized and accepted."""
    rm = make_risk_manager(
        tmp_path,
        default_snapshot(),
        open_lots=1,
        max_losses_per_symbol_per_day=1,
        losing_lots=0,
        addon_min_seconds_after_first_entry=0,
    )

    decision = rm.evaluate(make_signal())

    assert decision.accepted
    assert decision.sized_qty > 0
