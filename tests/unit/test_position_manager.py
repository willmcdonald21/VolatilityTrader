from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from tests.unit.fixtures import make_bars
from warrior_bot.config import BreakevenConfig, ExitsConfig, ReversalExitConfig, ScaleOutConfig, TrailingConfig
from warrior_bot.execution.position_manager import PositionManager
from warrior_bot.signals.signal import Signal


class FakeEvent:
    """Minimal stand-in for eventkit.Event -- `+=` appends a listener,
    `.emit(*args)` invokes every listener, matching how ib_async's
    Trade.fillEvent is used elsewhere in this codebase."""

    def __init__(self):
        self._listeners = []

    def __iadd__(self, listener):
        self._listeners.append(listener)
        return self

    def emit(self, *args) -> None:
        for listener in list(self._listeners):
            listener(*args)


class FakeOrder:
    def __init__(self, action, totalQuantity, auxPrice=None, lmtPrice=None, orderId=1):
        self.action = action
        self.totalQuantity = totalQuantity
        self.auxPrice = auxPrice
        self.lmtPrice = lmtPrice
        self.orderId = orderId


class FakeTrade:
    def __init__(self, order: FakeOrder):
        self.order = order
        self.fillEvent = FakeEvent()
        self.statusEvent = FakeEvent()


class FakeIB:
    def __init__(self):
        self.placed: list[tuple[object, FakeOrder]] = []
        self.cancelled: list[FakeOrder] = []

    def placeOrder(self, contract, order):
        self.placed.append((contract, order))
        return FakeTrade(order)

    def cancelOrder(self, order):
        self.cancelled.append(order)


class FakeJournal:
    def __init__(self):
        self.price_updates = []
        self.kill_switch_events = []

    def update_order_price(self, order_row_id, limit_price=None, stop_price=None, qty=None):
        self.price_updates.append((order_row_id, limit_price, stop_price, qty))

    def record_kill_switch_event(self, triggered_by, action_taken):
        self.kill_switch_events.append((triggered_by, action_taken))


class FakeCtx:
    """Duck-types the subset of SymbolContext's interface PositionManager
    actually reads, so these tests aren't coupled to real EMA/ATR bar math
    (that's covered separately in test_indicators.py)."""

    def __init__(self, symbol, last_price, ema_9=None, atr_value=None, bars=None):
        self.symbol = symbol
        self.last_price = last_price
        self.ema_9 = ema_9
        self._atr_value = atr_value
        self.bars = bars or []

    def atr(self, period=14):
        return self._atr_value


def make_signal(entry=10.0, stop=9.0, target=12.0) -> Signal:
    return Signal(
        symbol="TEST",
        strategy="gap_and_go",
        side="BUY",
        entry_price=entry,
        stop_price=stop,
        target_price=target,
        ts=datetime.now(timezone.utc),
    )


def make_fill(shares: float):
    return SimpleNamespace(execution=SimpleNamespace(shares=shares))


def make_exits_config(
    breakeven_enabled=True,
    breakeven_r=1.0,
    trailing_enabled=True,
    trailing_method="atr",
    atr_multiple=1.5,
    reversal_exit_enabled=False,
) -> ExitsConfig:
    return ExitsConfig(
        scale_out=ScaleOutConfig(),
        breakeven=BreakevenConfig(enabled=breakeven_enabled, trigger_r_multiple=breakeven_r),
        trailing=TrailingConfig(enabled=trailing_enabled, method=trailing_method, atr_multiple=atr_multiple),
        reversal_exit=ReversalExitConfig(enabled=reversal_exit_enabled),
    )


def track_position(pm: PositionManager, signal: Signal, quantity=100, target_role="target", target_qty=None):
    stop_order = FakeOrder("SELL", quantity, auxPrice=signal.stop_price, orderId=2)
    stop_trade = FakeTrade(stop_order)
    tq = target_qty if target_qty is not None else quantity
    target_order = FakeOrder("SELL", tq, lmtPrice=signal.target_price, orderId=3)
    target_trade = FakeTrade(target_order)
    pm.track(
        contract=object(),
        signal=signal,
        stop_trade=stop_trade,
        stop_row_id=1,
        target_trade=target_trade,
        target_role=target_role,
    )
    return stop_trade, target_trade


def test_breakeven_moves_stop_to_entry_once_r_multiple_reached():
    ib = FakeIB()
    pm = PositionManager(ib, FakeJournal(), make_exits_config(trailing_enabled=False))
    signal = make_signal(entry=10.0, stop=9.0)  # risk_per_share = 1.0
    stop_trade, _ = track_position(pm, signal)

    pm.on_bar(FakeCtx("TEST", last_price=10.5))  # +0.5R, not yet triggered
    assert stop_trade.order.auxPrice == 9.0

    pm.on_bar(FakeCtx("TEST", last_price=11.0))  # +1.0R, triggers breakeven
    assert stop_trade.order.auxPrice == 10.0
    assert any(order is stop_trade.order for _, order in ib.placed)


def test_breakeven_is_idempotent_no_duplicate_modify_calls():
    ib = FakeIB()
    pm = PositionManager(ib, FakeJournal(), make_exits_config(trailing_enabled=False))
    signal = make_signal(entry=10.0, stop=9.0)
    stop_trade, _ = track_position(pm, signal)

    pm.on_bar(FakeCtx("TEST", last_price=11.0))
    calls_after_trigger = len(ib.placed)
    assert calls_after_trigger == 1

    pm.on_bar(FakeCtx("TEST", last_price=11.0))
    assert len(ib.placed) == calls_after_trigger
    assert stop_trade.order.auxPrice == 10.0


def test_trailing_only_moves_stop_up_never_down():
    ib = FakeIB()
    pm = PositionManager(ib, FakeJournal(), make_exits_config(trailing_method="ema"))
    signal = make_signal(entry=10.0, stop=9.0)
    stop_trade, _ = track_position(pm, signal)

    # +1.0R triggers breakeven (stop -> 10.0) then, same bar, trailing
    # ratchets it further to the EMA candidate (10.5).
    pm.on_bar(FakeCtx("TEST", last_price=11.0, ema_9=10.5))
    assert stop_trade.order.auxPrice == 10.5

    # price and EMA keep rising -- stop trails up to 11.0
    pm.on_bar(FakeCtx("TEST", last_price=12.0, ema_9=11.0))
    assert stop_trade.order.auxPrice == 11.0

    # price pulls back and EMA drops below the current stop -- must NOT loosen
    pm.on_bar(FakeCtx("TEST", last_price=11.5, ema_9=10.8))
    assert stop_trade.order.auxPrice == 11.0


def test_trailing_cancels_static_target_once_activated():
    ib = FakeIB()
    pm = PositionManager(ib, FakeJournal(), make_exits_config(trailing_method="ema"))
    signal = make_signal(entry=10.0, stop=9.0)
    _, target_trade = track_position(pm, signal, target_role="target", target_qty=100)

    pm.on_bar(FakeCtx("TEST", last_price=11.0, ema_9=10.5))

    assert target_trade.order in ib.cancelled


def test_trailing_does_not_cancel_scale_out_leg():
    ib = FakeIB()
    pm = PositionManager(ib, FakeJournal(), make_exits_config(trailing_method="ema"))
    signal = make_signal(entry=10.0, stop=9.0)
    _, target_trade = track_position(pm, signal, quantity=100, target_role="scale_out", target_qty=40)

    pm.on_bar(FakeCtx("TEST", last_price=11.0, ema_9=10.5))

    assert target_trade.order not in ib.cancelled


def test_scale_out_fill_resizes_stop_quantity():
    ib = FakeIB()
    pm = PositionManager(ib, FakeJournal(), make_exits_config())
    signal = make_signal(entry=10.0, stop=9.0)
    stop_trade, target_trade = track_position(pm, signal, quantity=100, target_role="scale_out", target_qty=40)

    target_trade.fillEvent.emit(target_trade, make_fill(40))

    assert stop_trade.order.totalQuantity == 60
    assert any(order is stop_trade.order and order.totalQuantity == 60 for _, order in ib.placed)
    assert "TEST" in pm._positions  # remainder still tracked


def test_stop_fill_cancels_resting_scale_out_order():
    ib = FakeIB()
    pm = PositionManager(ib, FakeJournal(), make_exits_config())
    signal = make_signal(entry=10.0, stop=9.0)
    stop_trade, target_trade = track_position(pm, signal, quantity=100, target_role="scale_out", target_qty=40)

    stop_trade.fillEvent.emit(stop_trade, make_fill(100))

    assert target_trade.order in ib.cancelled
    assert "TEST" not in pm._positions


def test_full_target_fill_untracks_position_and_on_bar_is_a_noop_after():
    ib = FakeIB()
    pm = PositionManager(ib, FakeJournal(), make_exits_config())
    signal = make_signal(entry=10.0, stop=9.0)
    _, target_trade = track_position(pm, signal, quantity=100, target_role="target", target_qty=100)

    target_trade.fillEvent.emit(target_trade, make_fill(100))
    assert "TEST" not in pm._positions

    placed_before = len(ib.placed)
    pm.on_bar(FakeCtx("TEST", last_price=999.0, ema_9=999.0))  # would trigger breakeven/trailing if still tracked
    assert len(ib.placed) == placed_before


def test_reversal_exit_on_topping_tail_triggers_market_exit():
    ib = FakeIB()
    pm = PositionManager(ib, FakeJournal(), make_exits_config(reversal_exit_enabled=True))
    signal = make_signal(entry=10.0, stop=9.0)
    stop_trade, target_trade = track_position(pm, signal, quantity=100)

    bars = make_bars(
        [
            (10.0, 10.5, 9.9, 10.4, 1000),      # prior, green, unremarkable
            (10.4, 11.0, 10.35, 10.45, 1000),   # topping tail: tiny body, long upper wick
        ]
    )

    pm.on_bar(FakeCtx("TEST", last_price=10.45, bars=bars))

    assert stop_trade.order in ib.cancelled
    assert target_trade.order in ib.cancelled
    assert "TEST" not in pm._positions
    market_orders = [o for _, o in ib.placed if getattr(o, "orderType", None) == "MKT"]
    assert len(market_orders) == 1
    assert market_orders[0].totalQuantity == 100
    assert market_orders[0].action == "SELL"


def test_reversal_exit_on_red_after_green_triggers_market_exit():
    ib = FakeIB()
    pm = PositionManager(ib, FakeJournal(), make_exits_config(reversal_exit_enabled=True))
    signal = make_signal(entry=10.0, stop=9.0)
    stop_trade, target_trade = track_position(pm, signal, quantity=100)

    bars = make_bars(
        [
            (10.0, 10.5, 9.9, 10.4, 1000),   # prior, green
            (10.4, 10.5, 10.2, 10.25, 1000),  # red immediately after a green bar
        ]
    )

    pm.on_bar(FakeCtx("TEST", last_price=10.25, bars=bars))

    market_orders = [o for _, o in ib.placed if getattr(o, "orderType", None) == "MKT"]
    assert len(market_orders) == 1
    assert "TEST" not in pm._positions


def test_reversal_exit_on_volume_burst_triggers_market_exit():
    ib = FakeIB()
    pm = PositionManager(ib, FakeJournal(), make_exits_config(reversal_exit_enabled=True))
    signal = make_signal(entry=10.0, stop=9.0)
    stop_trade, target_trade = track_position(pm, signal, quantity=100)

    bars = make_bars(
        [
            (10.0, 10.1, 9.95, 10.0, 500),   # red, normal volume -- part of the "recent average"
            (10.0, 10.1, 9.95, 10.0, 500),
            (10.0, 10.05, 9.9, 9.95, 5000),  # red, volume well above the recent average
        ]
    )

    pm.on_bar(FakeCtx("TEST", last_price=9.95, bars=bars))

    market_orders = [o for _, o in ib.placed if getattr(o, "orderType", None) == "MKT"]
    assert len(market_orders) == 1
    assert "TEST" not in pm._positions


def test_reversal_exit_disabled_does_not_trigger():
    ib = FakeIB()
    pm = PositionManager(ib, FakeJournal(), make_exits_config(reversal_exit_enabled=False, trailing_enabled=False))
    signal = make_signal(entry=10.0, stop=9.0)
    track_position(pm, signal, quantity=100)

    bars = make_bars(
        [
            (10.0, 10.5, 9.9, 10.4, 1000),
            (10.4, 11.0, 10.35, 10.45, 1000),  # would be a topping tail if the feature were enabled
        ]
    )

    pm.on_bar(FakeCtx("TEST", last_price=10.45, bars=bars))

    market_orders = [o for _, o in ib.placed if getattr(o, "orderType", None) == "MKT"]
    assert len(market_orders) == 0
    assert "TEST" in pm._positions


def test_reversal_exit_no_pattern_leaves_position_untouched_and_runs_breakeven():
    ib = FakeIB()
    pm = PositionManager(ib, FakeJournal(), make_exits_config(reversal_exit_enabled=True, trailing_enabled=False))
    signal = make_signal(entry=10.0, stop=9.0)  # risk_per_share = 1.0
    stop_trade, _ = track_position(pm, signal, quantity=100)

    bars = make_bars(
        [
            (10.0, 10.5, 9.9, 10.4, 1000),   # green
            (10.4, 11.1, 10.35, 11.0, 1000),  # green, normal body -- no reversal pattern
        ]
    )

    pm.on_bar(FakeCtx("TEST", last_price=11.0, bars=bars))  # +1.0R -- breakeven should still fire

    market_orders = [o for _, o in ib.placed if getattr(o, "orderType", None) == "MKT"]
    assert len(market_orders) == 0
    assert "TEST" in pm._positions
    assert stop_trade.order.auxPrice == 10.0  # breakeven still ran since no reversal fired