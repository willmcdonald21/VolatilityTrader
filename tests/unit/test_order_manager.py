from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from warrior_bot.config import ExitsConfig, NotificationsConfig, ProfitTierConfig
from warrior_bot.execution import order_manager as order_manager_module
from warrior_bot.execution.order_manager import OrderManager
from warrior_bot.signals.signal import Signal


@pytest.fixture(autouse=True)
def _fast_entry_summary_debounce(monkeypatch):
    """Mirrors test_position_manager.py's _fast_debounced_resize fixture --
    _accumulate_entry_fill/_send_entry_summary debounce a burst of parent
    fills onto the event loop (see _ENTRY_SUMMARY_DEBOUNCE_SECONDS) so
    tests run synchronously with no loop driving itself, and
    flush_entry_summary below runs exactly the scheduled task on demand."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    monkeypatch.setattr(order_manager_module, "_ENTRY_SUMMARY_DEBOUNCE_SECONDS", 0)
    yield
    pending = asyncio.all_tasks(loop)
    for task in pending:
        task.cancel()
    if pending:
        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
    loop.close()


def flush_entry_summary(om: OrderManager, signal_id: int) -> None:
    task = om._entry_fill_state[signal_id]["task"]
    if task is not None:
        asyncio.get_event_loop().run_until_complete(task)


class FakePositionManager:
    """Duck-types the subset of PositionManager's interface
    _send_entry_summary actually reads -- `lots` maps symbol -> list of
    (signal_id, avg_price) tuples representing OTHER already-open lots for
    that symbol, letting a test simulate a pyramid add-on without needing
    a real PositionManager/bracket submission."""

    def __init__(self, lots: dict[str, list[tuple[int, float]]] | None = None):
        self._lots = lots or {}

    def other_open_lot(self, symbol, exclude_signal_id):
        for signal_id, _avg_price in self._lots.get(symbol, []):
            if signal_id != exclude_signal_id:
                return SimpleNamespace(signal_id=signal_id)
        return None


class FakeEvent:
    def __init__(self):
        self._listeners = []

    def __iadd__(self, listener):
        self._listeners.append(listener)
        return self

    def emit(self, *args) -> None:
        for listener in list(self._listeners):
            listener(*args)


class FakeOrder:
    def __init__(self, action="SELL", orderId=1, totalQuantity=100):
        self.action = action
        self.orderId = orderId
        self.totalQuantity = totalQuantity


class FakeContract:
    def __init__(self, symbol="AAPL"):
        self.symbol = symbol


class FakeTrade:
    def __init__(self, order=None, symbol="AAPL"):
        self.order = order or FakeOrder()
        self.contract = FakeContract(symbol)
        self.fillEvent = FakeEvent()
        self.statusEvent = FakeEvent()
        self.orderStatus = SimpleNamespace(status="Submitted")


class FakeIB:
    def __init__(self, trades):
        self._trades = trades

    def openTrades(self):
        return self._trades


class FakeJournal:
    def __init__(self, orders_by_ib_id=None):
        self.fills = []
        self._orders_by_ib_id = orders_by_ib_id or {}

    def find_order_by_ib_order_id(self, ib_order_id):
        return self._orders_by_ib_id.get(ib_order_id)

    def update_order_status(self, row_id, status):
        pass

    def record_fill(self, order_row_id, ib_order_id, fill_qty, fill_price, commission, realized_pnl):
        self.fills.append(
            {
                "order_row_id": order_row_id,
                "fill_qty": fill_qty,
                "fill_price": fill_price,
                "commission": commission,
                "realized_pnl": realized_pnl,
            }
        )


class FakeAccountState:
    def __init__(self, daily_realized_pnl=0.0):
        self._snapshot = SimpleNamespace(daily_realized_pnl=daily_realized_pnl)

    def snapshot(self):
        return self._snapshot


def make_fill(shares=100, price=10.0, realized_pnl=None):
    commission_report = None
    if realized_pnl is not None:
        commission_report = SimpleNamespace(commission=1.0, realizedPNL=realized_pnl)
    return SimpleNamespace(execution=SimpleNamespace(shares=shares, price=price), commissionReport=commission_report)


def make_order_manager(
    notifications_enabled=True, daily_realized_pnl=0.0, position_manager=None, trading_mode="paper", **notif_overrides
):
    notifications_config = NotificationsConfig(enabled=notifications_enabled, **notif_overrides)
    return OrderManager(
        ib=None,
        journal=FakeJournal(),
        exits_config=None,
        position_manager=position_manager or FakePositionManager(),
        notifications_config=notifications_config,
        account_state=FakeAccountState(daily_realized_pnl),
        trading_mode=trading_mode,
    )


def _capture_sends(monkeypatch, om):
    sent = []
    monkeypatch.setattr(
        "warrior_bot.execution.order_manager.send_discord_message",
        lambda content, channel: sent.append((content, channel)),
    )
    return sent


def _by_channel(sent, channel):
    return [content for content, ch in sent if ch == channel]


def _capture_embeds(monkeypatch, om):
    sent = []
    monkeypatch.setattr(
        "warrior_bot.execution.order_manager.send_discord_embed",
        lambda embed, channel: sent.append((embed, channel)),
    )
    return sent


def _seed_entry_state(om, signal_id, symbol="AAPL", strategy="gap_and_go", stop_price=9.0, trim_targets=None):
    om._entry_fill_state[signal_id] = {
        "symbol": symbol,
        "strategy": strategy,
        "stop_price": stop_price,
        "trim_targets": trim_targets if trim_targets is not None else [(1.0, 12.0)],
        "qty": 0.0,
        "notional": 0.0,
        "avg_price": None,
        "task": None,
    }


def test_entry_fill_labeled_buy_no_pnl_message(monkeypatch):
    om = make_order_manager()
    sent = _capture_sends(monkeypatch, om)
    trade = FakeTrade(FakeOrder(action="BUY"))
    om._attach_tracking(trade, row_id=1, role="parent")

    trade.fillEvent.emit(trade, make_fill(shares=100, price=5.5, realized_pnl=None))

    assert len(sent) == 1
    assert "BUY AAPL 100 @ $5.50" in sent[0][0]
    assert sent[0][1] == "trade_activity"
    assert _by_channel(sent, "pnl") == []  # no realized P&L on the opening leg -- no pnl channel post


def test_full_exit_fill_labeled_sell_with_pnl(monkeypatch):
    om = make_order_manager(daily_realized_pnl=340.5)
    sent = _capture_sends(monkeypatch, om)
    trade = FakeTrade(FakeOrder(action="SELL"))
    om._attach_tracking(trade, row_id=1, role="target")

    trade.fillEvent.emit(trade, make_fill(shares=100, price=6.08, realized_pnl=114.0))

    trade_activity = _by_channel(sent, "trade_activity")
    assert "SELL AAPL 100 @ $6.08 (P&L $114.00)" in trade_activity[0]

    pnl_messages = _by_channel(sent, "pnl")
    assert pnl_messages == ["📈 AAPL: +$114.00\n📈 Daily P&L: +$340.50"]


def test_stop_exit_fill_also_labeled_sell(monkeypatch):
    om = make_order_manager()
    sent = _capture_sends(monkeypatch, om)
    trade = FakeTrade(FakeOrder(action="SELL"))
    om._attach_tracking(trade, row_id=1, role="stop")

    trade.fillEvent.emit(trade, make_fill(realized_pnl=-50.0))

    trade_activity = _by_channel(sent, "trade_activity")
    assert "SELL AAPL" in trade_activity[0]
    pnl_messages = _by_channel(sent, "pnl")
    assert pnl_messages[0].startswith("📉 AAPL: -$50.00")


def test_scale_out_fill_labeled_trim(monkeypatch):
    om = make_order_manager()
    sent = _capture_sends(monkeypatch, om)
    trade = FakeTrade(FakeOrder(action="SELL"))
    om._attach_tracking(trade, row_id=1, role="scale_out", entry_price=5.0)

    trade.fillEvent.emit(trade, make_fill(shares=50, price=6.0, realized_pnl=25.0))

    trade_activity = _by_channel(sent, "trade_activity")
    assert "TRIM AAPL 50 @ $6.00" in trade_activity[0]
    assert len(_by_channel(sent, "pnl")) == 1  # trims realize P&L too -- still posts to the pnl channel


def test_trim_message_includes_pct_gain_from_entry(monkeypatch):
    om = make_order_manager()
    sent = _capture_sends(monkeypatch, om)
    trade = FakeTrade(FakeOrder(action="SELL"))
    om._attach_tracking(trade, row_id=1, role="scale_out", entry_price=1.79)

    trade.fillEvent.emit(trade, make_fill(shares=1000, price=1.93, realized_pnl=140.0))

    trade_activity = _by_channel(sent, "trade_activity")
    assert "(+7.8% from entry)" in trade_activity[0]


def test_trim_message_pct_gain_negative_when_below_entry(monkeypatch):
    om = make_order_manager()
    sent = _capture_sends(monkeypatch, om)
    trade = FakeTrade(FakeOrder(action="SELL"))
    om._attach_tracking(trade, row_id=1, role="scale_out", entry_price=10.0)

    trade.fillEvent.emit(trade, make_fill(shares=50, price=9.0, realized_pnl=-50.0))

    trade_activity = _by_channel(sent, "trade_activity")
    assert "(-10.0% from entry)" in trade_activity[0]


def test_non_trim_fills_do_not_include_pct_gain(monkeypatch):
    om = make_order_manager()
    sent = _capture_sends(monkeypatch, om)
    trade = FakeTrade(FakeOrder(action="SELL"))
    om._attach_tracking(trade, row_id=1, role="target", entry_price=5.0)

    trade.fillEvent.emit(trade, make_fill(shares=100, price=6.0, realized_pnl=100.0))

    trade_activity = _by_channel(sent, "trade_activity")
    assert "from entry" not in trade_activity[0]


def test_no_messages_when_notifications_disabled(monkeypatch):
    om = make_order_manager(notifications_enabled=False)
    sent = _capture_sends(monkeypatch, om)
    trade = FakeTrade(FakeOrder(action="BUY"))
    om._attach_tracking(trade, row_id=1, role="parent")

    trade.fillEvent.emit(trade, make_fill())

    assert sent == []


def test_pnl_channel_respects_its_own_flag(monkeypatch):
    om = make_order_manager(notify_on_pnl=False)
    sent = _capture_sends(monkeypatch, om)
    trade = FakeTrade(FakeOrder(action="SELL"))
    om._attach_tracking(trade, row_id=1, role="target")

    trade.fillEvent.emit(trade, make_fill(realized_pnl=10.0))

    assert len(_by_channel(sent, "trade_activity")) == 1  # trade_activity still fires
    assert _by_channel(sent, "pnl") == []  # pnl channel does not


def test_fill_always_journaled_regardless_of_notifications(monkeypatch):
    om = make_order_manager(notifications_enabled=False)
    _capture_sends(monkeypatch, om)
    trade = FakeTrade(FakeOrder(action="BUY"))
    om._attach_tracking(trade, row_id=7, role="parent")

    trade.fillEvent.emit(trade, make_fill(shares=100, price=5.5))

    assert len(om.journal.fills) == 1
    assert om.journal.fills[0]["order_row_id"] == 7


def test_resync_reattaches_tracking_to_pre_existing_open_orders(monkeypatch):
    stop_trade = FakeTrade(FakeOrder(action="SELL", orderId=51004), symbol="VHUB")
    journal = FakeJournal(orders_by_ib_id={51004: {"row_id": 470, "role": "stop", "entry_price": 1.01}})
    om = OrderManager(
        ib=FakeIB([stop_trade]),
        journal=journal,
        exits_config=None,
        position_manager=None,
        notifications_config=NotificationsConfig(enabled=True),
        account_state=FakeAccountState(),
    )
    sent = _capture_sends(monkeypatch, om)

    om.resync_open_orders()
    stop_trade.fillEvent.emit(stop_trade, make_fill(shares=2049, price=0.99))

    assert len(journal.fills) == 1
    assert journal.fills[0]["order_row_id"] == 470
    assert len(_by_channel(sent, "trade_activity")) == 1
    assert 51004 in om._order_row_ids


def test_resync_skips_orders_not_in_journal(monkeypatch):
    # A manually-placed order (e.g. a hand-restored protective stop) has
    # no signal_id to attach fills to -- resync must not raise or attach
    # anything for it, just leave it untracked.
    manual_trade = FakeTrade(FakeOrder(action="SELL", orderId=51235), symbol="VHUB")
    om = OrderManager(
        ib=FakeIB([manual_trade]),
        journal=FakeJournal(),
        exits_config=None,
        position_manager=None,
        notifications_config=NotificationsConfig(enabled=True),
        account_state=FakeAccountState(),
    )

    om.resync_open_orders()

    assert 51235 not in om._order_row_ids


def test_resync_does_not_double_attach_already_tracked_orders(monkeypatch):
    trade = FakeTrade(FakeOrder(action="SELL", orderId=10))
    journal = FakeJournal(orders_by_ib_id={10: {"row_id": 99, "role": "stop", "entry_price": 5.0}})
    om = OrderManager(
        ib=FakeIB([trade]),
        journal=journal,
        exits_config=None,
        position_manager=None,
        notifications_config=NotificationsConfig(enabled=True),
        account_state=FakeAccountState(),
    )
    om._order_row_ids[10] = 99  # already attached this session (e.g. just placed)
    om._attach_tracking(trade, row_id=99, role="stop")  # the attachment resync must not duplicate

    om.resync_open_orders()
    trade.fillEvent.emit(trade, make_fill(shares=50, price=5.0))

    # Only one listener ever got attached -- one fill, not a double-record.
    assert len(journal.fills) == 1


def test_profit_tier_prices_are_tick_conformant():
    # entry + risk_per_share * r_multiple is raw float arithmetic -- pick
    # values that produce a many-decimal result and confirm each tier's
    # price comes out rounded to a valid $0.01 tick before it would reach
    # IBKR.
    om = OrderManager(
        ib=None,
        journal=FakeJournal(),
        exits_config=ExitsConfig(
            profit_tiers=[
                ProfitTierConfig(r_multiple=1.37, pct=0.34),
                ProfitTierConfig(r_multiple=2.53, pct=0.33),
            ]
        ),
        position_manager=None,
    )
    signal = Signal(
        symbol="TEST",
        strategy="gap_and_go",
        side="BUY",
        entry_price=13.47,
        stop_price=13.2832,
        target_price=13.9,
        ts=datetime.now(timezone.utc),
    )
    specs = om._profit_tier_specs(signal, quantity=100)
    assert len(specs) == 2
    for qty, price in specs:
        assert price == round(price, 2)

def test_resync_force_reattaches_orders_already_known_from_before_reconnect():
    # The reconnect case (main.py's _on_connected): this instance already
    # placed this order and has its orderId in _order_row_ids from before
    # ib_async's reconnect wiped its Trade-object cache -- the plain
    # `if order_id in self._order_row_ids: continue` guard would wrongly
    # treat that as "already has a live listener" when the listener it
    # actually has is wired to a dead, pre-reconnect Trade object.
    # force=True must re-attach onto the fresh Trade regardless.
    journal = FakeJournal(orders_by_ib_id={10: {"row_id": 99, "role": "stop", "entry_price": 5.0}})
    om = OrderManager(
        ib=None,
        journal=journal,
        exits_config=None,
        position_manager=None,
        notifications_config=NotificationsConfig(enabled=False),
        account_state=FakeAccountState(),
    )
    om._order_row_ids[10] = 99  # known from before the reconnect

    fresh_trade = FakeTrade(FakeOrder(action="SELL", orderId=10))
    om.ib = FakeIB([fresh_trade])

    om.resync_open_orders(force=True)
    fresh_trade.fillEvent.emit(fresh_trade, make_fill(shares=50, price=5.0))

    assert len(journal.fills) == 1  # the fresh Trade's listener is live


def test_resync_without_force_skips_already_known_orders():
    journal = FakeJournal(orders_by_ib_id={10: {"row_id": 99, "role": "stop", "entry_price": 5.0}})
    om = OrderManager(
        ib=None,
        journal=journal,
        exits_config=None,
        position_manager=None,
        notifications_config=NotificationsConfig(enabled=False),
        account_state=FakeAccountState(),
    )
    om._order_row_ids[10] = 99
    fresh_trade = FakeTrade(FakeOrder(action="SELL", orderId=10))
    om.ib = FakeIB([fresh_trade])

    om.resync_open_orders(force=False)
    fresh_trade.fillEvent.emit(fresh_trade, make_fill(shares=50, price=5.0))

    assert len(journal.fills) == 0  # not re-attached -- default (non-reconnect) behavior unchanged


def test_resync_skip_order_ids_excludes_positions_already_claimed():
    # Coordinates with PositionManager.resync_after_reconnect: an orderId
    # it already re-wired itself (and will journal itself) must not also
    # get OrderManager's own journaling listener, or the next fill on it
    # double-journals -- the exact bug already fixed once in
    # track()/_wire_stop_fill for the non-reconnect path.
    journal = FakeJournal(orders_by_ib_id={10: {"row_id": 99, "role": "stop", "entry_price": 5.0}})
    om = OrderManager(
        ib=None,
        journal=journal,
        exits_config=None,
        position_manager=None,
        notifications_config=NotificationsConfig(enabled=False),
        account_state=FakeAccountState(),
    )
    fresh_trade = FakeTrade(FakeOrder(action="SELL", orderId=10))
    om.ib = FakeIB([fresh_trade])

    om.resync_open_orders(force=True, skip_order_ids=frozenset({10}))
    fresh_trade.fillEvent.emit(fresh_trade, make_fill(shares=50, price=5.0))

    assert len(journal.fills) == 0
    assert 10 not in om._order_row_ids


# -- trade_activity_summary: debounced entry-fill-burst embed --
# Regression coverage for the 2026-09-22 report of "duplicate" BUY lines in
# Discord -- confirmed (via IBKR's own execution report) to be genuine
# separate partial fills of one parent order, not a bug, but visually noisy.
# This coalesces a burst of parent fills into one embed per entry.


def test_accumulate_entry_fill_schedules_and_sends_after_debounce(monkeypatch):
    om = make_order_manager()
    embeds = _capture_embeds(monkeypatch, om)
    _seed_entry_state(om, signal_id=1, symbol="JTAI", stop_price=2.02, trim_targets=[(0.34, 2.19)])

    om._accumulate_entry_fill(1, fill_qty=100.0, fill_price=2.11)
    flush_entry_summary(om, 1)

    assert len(embeds) == 1
    embed, channel = embeds[0]
    assert channel == "trade_activity_summary"
    assert "JTAI" in embed["title"]


def test_accumulate_entry_fill_coalesces_a_burst_into_one_embed(monkeypatch):
    # Mirrors the real JTAI incident: 100+100+100+842+200+140 = 1482,
    # landing as 6 separate fillEvent calls -- must produce exactly one
    # embed with the combined totals, not six.
    om = make_order_manager()
    embeds = _capture_embeds(monkeypatch, om)
    _seed_entry_state(om, signal_id=1, symbol="JTAI")

    for qty in (100.0, 100.0, 100.0, 842.0, 200.0, 140.0):
        om._accumulate_entry_fill(1, fill_qty=qty, fill_price=2.11)  # same debounce task rescheduled each time
    flush_entry_summary(om, 1)

    assert len(embeds) == 1
    field_by_name = {f["name"]: f["value"] for f in embeds[0][0]["fields"]}
    assert field_by_name["Shares"] == "1482"
    assert field_by_name["Avg Price"] == "$2.11"


def test_accumulate_entry_fill_weighted_average_across_different_prices(monkeypatch):
    om = make_order_manager()
    embeds = _capture_embeds(monkeypatch, om)
    _seed_entry_state(om, signal_id=1, symbol="X")

    om._accumulate_entry_fill(1, fill_qty=100.0, fill_price=2.00)
    om._accumulate_entry_fill(1, fill_qty=300.0, fill_price=2.10)
    flush_entry_summary(om, 1)

    # (100*2.00 + 300*2.10) / 400 = 2.075 -> $2.08 (banker's/round-half rounding via f-string)
    field_by_name = {f["name"]: f["value"] for f in embeds[0][0]["fields"]}
    assert field_by_name["Avg Price"] == "$2.08"
    assert field_by_name["Shares"] == "400"


def test_send_entry_summary_frames_second_lot_as_addon(monkeypatch):
    pm = FakePositionManager(lots={"GDC": [(1, 2.00)]})  # an existing open lot, signal_id=1, avg $2.00
    om = make_order_manager(position_manager=pm)
    embeds = _capture_embeds(monkeypatch, om)
    _seed_entry_state(om, signal_id=1, symbol="GDC")
    om._accumulate_entry_fill(1, fill_qty=200.0, fill_price=2.00)
    flush_entry_summary(om, 1)  # settles lot 1's own accumulator (avg_price=2.00) first
    embeds.clear()

    _seed_entry_state(om, signal_id=2, symbol="GDC")
    om._accumulate_entry_fill(2, fill_qty=100.0, fill_price=2.20)
    flush_entry_summary(om, 2)

    assert len(embeds) == 1
    embed = embeds[0][0]
    assert "ADD TO POSITION" in embed["title"]


def test_send_entry_summary_first_lot_is_new_position_not_addon(monkeypatch):
    om = make_order_manager(position_manager=FakePositionManager())  # no other lots anywhere
    embeds = _capture_embeds(monkeypatch, om)
    _seed_entry_state(om, signal_id=1, symbol="GDC")

    om._accumulate_entry_fill(1, fill_qty=100.0, fill_price=2.00)
    flush_entry_summary(om, 1)

    assert "NEW POSITION" in embeds[0][0]["title"]


def test_send_entry_summary_falls_back_to_new_position_if_prior_state_missing(monkeypatch):
    # other_open_lot reports a second lot, but this OrderManager instance
    # never saw its fills (e.g. process restarted mid-day) -- must degrade
    # gracefully to a plain "new position" framing, not crash or fabricate
    # a prior average.
    pm = FakePositionManager(lots={"GDC": [(99, 2.00)]})  # signal_id 99 unknown to this OrderManager
    om = make_order_manager(position_manager=pm)
    embeds = _capture_embeds(monkeypatch, om)
    _seed_entry_state(om, signal_id=1, symbol="GDC")

    om._accumulate_entry_fill(1, fill_qty=100.0, fill_price=2.00)
    flush_entry_summary(om, 1)

    assert "NEW POSITION" in embeds[0][0]["title"]


def test_send_entry_summary_respects_notify_on_entry_summary_toggle(monkeypatch):
    om = make_order_manager(notify_on_entry_summary=False)
    embeds = _capture_embeds(monkeypatch, om)
    _seed_entry_state(om, signal_id=1, symbol="X")

    om._accumulate_entry_fill(1, fill_qty=100.0, fill_price=2.0)
    flush_entry_summary(om, 1)

    assert embeds == []


def test_send_entry_summary_noop_when_notifications_disabled(monkeypatch):
    om = make_order_manager(notifications_enabled=False)
    embeds = _capture_embeds(monkeypatch, om)
    _seed_entry_state(om, signal_id=1, symbol="X")

    om._accumulate_entry_fill(1, fill_qty=100.0, fill_price=2.0)
    flush_entry_summary(om, 1)

    assert embeds == []


def test_attach_tracking_without_signal_id_does_not_accumulate(monkeypatch):
    # resync_open_orders' path (a parent order still working across a
    # process restart) calls _attach_tracking with no signal_id -- must not
    # touch _entry_fill_state or crash on a missing accumulator.
    om = make_order_manager()
    embeds = _capture_embeds(monkeypatch, om)
    trade = FakeTrade(FakeOrder(action="BUY"))

    om._attach_tracking(trade, row_id=1, role="parent")  # no signal_id
    trade.fillEvent.emit(trade, make_fill(shares=100, price=2.0))

    assert om._entry_fill_state == {}
    assert embeds == []


def test_accumulate_entry_fill_noop_for_unknown_signal_id():
    om = make_order_manager()
    om._accumulate_entry_fill(999, fill_qty=100.0, fill_price=2.0)  # never seeded -- must not raise
    assert om._entry_fill_state == {}


def test_non_parent_fill_does_not_accumulate_or_send_embed(monkeypatch):
    om = make_order_manager()
    embeds = _capture_embeds(monkeypatch, om)
    trade = FakeTrade(FakeOrder(action="SELL"))
    _seed_entry_state(om, signal_id=1, symbol="X")

    om._attach_tracking(trade, row_id=1, role="stop", signal_id=1)
    trade.fillEvent.emit(trade, make_fill(shares=100, price=2.0))

    assert om._entry_fill_state[1]["qty"] == 0.0  # stop fill never touches the entry accumulator
    assert embeds == []
