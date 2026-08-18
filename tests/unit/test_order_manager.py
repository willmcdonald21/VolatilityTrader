from __future__ import annotations

from types import SimpleNamespace

from warrior_bot.config import NotificationsConfig
from warrior_bot.execution.order_manager import OrderManager


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


class FakeJournal:
    def __init__(self):
        self.fills = []

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
    om._attach_tracking(trade, row_id=1, role="scale_out")

    trade.fillEvent.emit(trade, make_fill(shares=50, price=6.0, realized_pnl=25.0))

    trade_activity = _by_channel(sent, "trade_activity")
    assert "TRIM AAPL 50 @ $6.00" in trade_activity[0]
    assert len(_by_channel(sent, "pnl")) == 1  # trims realize P&L too -- still posts to the pnl channel


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
