from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from warrior_bot.config import ExitsConfig, NotificationsConfig, ProfitTierConfig
from warrior_bot.execution.order_manager import OrderManager
from warrior_bot.signals.signal import Signal


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


def make_order_manager(notifications_enabled=True, daily_realized_pnl=0.0, **notif_overrides):
    notifications_config = NotificationsConfig(enabled=notifications_enabled, **notif_overrides)
    return OrderManager(
        ib=None,
        journal=FakeJournal(),
        exits_config=None,
        position_manager=None,
        notifications_config=notifications_config,
        account_state=FakeAccountState(daily_realized_pnl),
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
