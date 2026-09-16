from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from tests.unit.fixtures import make_bars
from warrior_bot.config import BreakevenConfig, ExitsConfig, ReversalExitConfig, TrailingConfig
from warrior_bot.execution import position_manager as position_manager_module
from warrior_bot.execution.position_manager import PositionManager
from warrior_bot.signals.signal import Signal


@pytest.fixture(autouse=True)
def _fast_debounced_resize(monkeypatch):
    """Quantity-only stop resizes (on_entry_fill/_on_target_fill) are
    debounced onto the event loop (see _schedule_stop_resize) so a burst of
    fills collapses into one replace instead of one per fill. Tests run
    synchronously with no loop driving itself, so this gives each test its
    own loop and a zero-length debounce -- `flush_resize(pos)` below then
    runs exactly the scheduled task to completion on demand."""
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


def flush_resize(pos) -> None:
    """Runs a pending debounced stop resize (see _schedule_stop_resize) to
    completion. No-op if nothing is pending."""
    if pos.resize_task is not None:
        asyncio.get_event_loop().run_until_complete(pos.resize_task)


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
    def __init__(
        self,
        action,
        totalQuantity,
        auxPrice=None,
        lmtPrice=None,
        orderId=1,
        orderType=None,
        ocaGroup="",
        parentId=0,
    ):
        self.action = action
        self.totalQuantity = totalQuantity
        self.auxPrice = auxPrice
        self.lmtPrice = lmtPrice
        self.orderId = orderId
        self.orderType = orderType
        self.ocaGroup = ocaGroup
        self.parentId = parentId


class FakeTrade:
    def __init__(self, order: FakeOrder, remaining: float = 0):
        self.order = order
        self.fillEvent = FakeEvent()
        self.statusEvent = FakeEvent()
        # remaining=0 by default -- most fixtures build a Trade to represent
        # an order that's already fully resolved one way or another; tests
        # simulating a still-filling parent set this explicitly before each
        # fillEvent.emit() (matching real IBKR orderStatus.remaining).
        self.orderStatus = SimpleNamespace(status="Submitted", remaining=remaining)


class FakeClient:
    """getReqId() incrementing from a base clearly outside the 1-10 range
    tests hand-assign to their own FakeOrders, so replacement orderIds
    never collide with a test's own fixture orderIds."""

    def __init__(self):
        self._next_id = 100

    def getReqId(self) -> int:
        self._next_id += 1
        return self._next_id


class FakeIB:
    def __init__(self):
        self.placed: list[tuple[object, FakeOrder]] = []
        self.cancelled: list[FakeOrder] = []
        self.trades: list[FakeTrade] = []
        self.client = FakeClient()

    def placeOrder(self, contract, order):
        self.placed.append((contract, order))
        trade = FakeTrade(order)
        self.trades.append(trade)
        return trade

    def cancelOrder(self, order):
        self.cancelled.append(order)


def find_trade(ib: FakeIB, order: FakeOrder) -> FakeTrade:
    """Looks up the FakeTrade wrapping `order` -- used to reach the fresh
    Trade a cancel-and-replace stop revision created, since PositionManager
    re-wires its fill listener onto that new object, not the original."""
    return next(t for t in ib.trades if t.order is order)


class FakeJournal:
    def __init__(self):
        self.price_updates = []
        self.kill_switch_events = []
        self.orders_recorded = []
        self.order_statuses = []
        self.fills_recorded = []
        self._next_row_id = 100

    def update_order_price(self, order_row_id, limit_price=None, stop_price=None, qty=None):
        self.price_updates.append((order_row_id, limit_price, stop_price, qty))

    def record_fill(self, **kwargs):
        self.fills_recorded.append(kwargs)
        return len(self.fills_recorded)

    def record_kill_switch_event(self, triggered_by, action_taken):
        self.kill_switch_events.append((triggered_by, action_taken))

    def record_order(self, **kwargs):
        self.orders_recorded.append(kwargs)
        self._next_row_id += 1
        return self._next_row_id

    def update_order_status(self, order_row_id, status):
        self.order_statuses.append((order_row_id, status))


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


def make_fill(shares: float, price: float = 0.0, commission_report=None):
    return SimpleNamespace(
        execution=SimpleNamespace(shares=shares, price=price), commissionReport=commission_report
    )


def make_exits_config(
    breakeven_enabled=True,
    breakeven_r=1.0,
    trailing_enabled=True,
    trailing_method="atr",
    atr_multiple=1.5,
    reversal_exit_enabled=False,
) -> ExitsConfig:
    return ExitsConfig(
        breakeven=BreakevenConfig(enabled=breakeven_enabled, trigger_r_multiple=breakeven_r),
        trailing=TrailingConfig(enabled=trailing_enabled, method=trailing_method, atr_multiple=atr_multiple),
        reversal_exit=ReversalExitConfig(enabled=reversal_exit_enabled),
    )


def track_position(
    pm: PositionManager,
    signal: Signal,
    quantity=100,
    target_role="target",
    target_qty=None,
    stop_order_type=None,
    order_id_offset=0,
    signal_id=1,
    entry_filled=True,
):
    parent_order = FakeOrder("BUY", quantity, lmtPrice=signal.entry_price, orderId=1 + order_id_offset)
    parent_trade = FakeTrade(parent_order)
    stop_order = FakeOrder(
        "SELL",
        quantity,
        auxPrice=signal.stop_price,
        orderId=2 + order_id_offset,
        orderType=stop_order_type,
        parentId=parent_order.orderId,
    )
    stop_trade = FakeTrade(stop_order)
    tq = target_qty if target_qty is not None else quantity
    target_order = FakeOrder("SELL", tq, lmtPrice=signal.target_price, orderId=3 + order_id_offset)
    if target_role == "target":
        target_order.ocaGroup = f"TEST-{order_id_offset}-OCA"
    target_trade = FakeTrade(target_order)
    pm.track(
        contract=object(),
        signal=signal,
        signal_id=signal_id,
        parent_trade=parent_trade,
        stop_trade=stop_trade,
        stop_row_id=1,
        target_trades=[target_trade],
        target_roles=[target_role],
    )
    if entry_filled:
        parent_trade.fillEvent.emit(parent_trade, make_fill(quantity))
        flush_resize(pm._positions[signal.symbol][-1])
    return stop_trade, target_trade


def test_breakeven_moves_stop_to_entry_once_r_multiple_reached():
    ib = FakeIB()
    pm = PositionManager(ib, FakeJournal(), make_exits_config(trailing_enabled=False))
    signal = make_signal(entry=10.0, stop=9.0)  # risk_per_share = 1.0
    # track_position's entry_filled=True already cancel-and-replaces the
    # original stop once (on_entry_fill), so the "current" stop at this
    # point is already a second-generation order -- pos.stop_order, not
    # the stale stop_trade.order this helper returns.
    stop_trade, _ = track_position(pm, signal)
    stop_after_entry_fill = pm._positions["TEST"][0].stop_order

    pm.on_bar(FakeCtx("TEST", last_price=10.5))  # +0.5R, not yet triggered
    assert pm._positions["TEST"][0].current_stop_price == 9.0
    assert stop_after_entry_fill not in ib.cancelled

    pm.on_bar(FakeCtx("TEST", last_price=11.0))  # +1.0R, triggers breakeven
    pos = pm._positions["TEST"][0]
    assert pos.current_stop_price == 10.0
    assert pos.stop_order.auxPrice == 10.0
    assert stop_after_entry_fill in ib.cancelled  # cancel-and-replace, not in-place modify
    assert any(order is pos.stop_order for _, order in ib.placed)


def test_breakeven_is_idempotent_no_duplicate_modify_calls():
    ib = FakeIB()
    pm = PositionManager(ib, FakeJournal(), make_exits_config(trailing_enabled=False))
    signal = make_signal(entry=10.0, stop=9.0)
    # track_position's entry_filled=True already places 1 order (the
    # entry-fill stop resize -- see on_entry_fill); breakeven adds one more.
    stop_trade, _ = track_position(pm, signal)
    calls_before_trigger = len(ib.placed)

    pm.on_bar(FakeCtx("TEST", last_price=11.0))
    calls_after_trigger = len(ib.placed)
    assert calls_after_trigger == calls_before_trigger + 1

    pm.on_bar(FakeCtx("TEST", last_price=11.0))
    assert len(ib.placed) == calls_after_trigger
    assert pm._positions["TEST"][0].current_stop_price == 10.0


def test_trailing_only_moves_stop_up_never_down():
    ib = FakeIB()
    pm = PositionManager(ib, FakeJournal(), make_exits_config(trailing_method="ema"))
    signal = make_signal(entry=10.0, stop=9.0)
    stop_trade, _ = track_position(pm, signal)

    # +1.0R triggers breakeven (stop -> 10.0) then, same bar, trailing
    # ratchets it further to the EMA candidate (10.5).
    pm.on_bar(FakeCtx("TEST", last_price=11.0, ema_9=10.5))
    assert pm._positions["TEST"][0].current_stop_price == 10.5

    # price and EMA keep rising -- stop trails up to 11.0
    pm.on_bar(FakeCtx("TEST", last_price=12.0, ema_9=11.0))
    assert pm._positions["TEST"][0].current_stop_price == 11.0

    # price pulls back and EMA drops below the current stop -- must NOT loosen
    pm.on_bar(FakeCtx("TEST", last_price=11.5, ema_9=10.8))
    assert pm._positions["TEST"][0].current_stop_price == 11.0


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
    # entry_filled=True already cancel-and-replaces the stop once
    # (on_entry_fill) -- the resize under test is a *second* replacement.
    _, target_trade = track_position(pm, signal, quantity=100, target_role="scale_out", target_qty=40)

    target_trade.fillEvent.emit(target_trade, make_fill(40))

    pos = pm._positions["TEST"][0]
    flush_resize(pos)
    assert pos.stop_order.totalQuantity == 60
    assert any(order is pos.stop_order and order.totalQuantity == 60 for _, order in ib.placed)
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


def test_reversal_exit_on_momentum_exhaustion_triggers_market_exit():
    ib = FakeIB()
    pm = PositionManager(ib, FakeJournal(), make_exits_config(reversal_exit_enabled=True))
    signal = make_signal(entry=10.0, stop=9.0)
    stop_trade, target_trade = track_position(pm, signal, quantity=100)

    # three green bars, shrinking body AND shrinking volume -- none of the
    # other reversal-exit checks (topping tail, red-after-green, lower
    # low, volume burst) fire on this shape, isolating momentum exhaustion
    bars = make_bars(
        [
            (10.0, 10.95, 9.95, 10.9, 3000),
            (10.9, 11.55, 10.85, 11.5, 2000),
            (11.5, 11.85, 11.45, 11.8, 1000),
        ]
    )

    pm.on_bar(FakeCtx("TEST", last_price=11.8, bars=bars))

    assert stop_trade.order in ib.cancelled
    assert target_trade.order in ib.cancelled
    assert "TEST" not in pm._positions
    market_orders = [o for _, o in ib.placed if getattr(o, "orderType", None) == "MKT"]
    assert len(market_orders) == 1


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
    assert pm._positions["TEST"][0].current_stop_price == 10.0  # breakeven still ran since no reversal fired


def test_breakeven_updates_stop_limit_price_when_order_is_stop_limit():
    ib = FakeIB()
    pm = PositionManager(ib, FakeJournal(), make_exits_config(trailing_enabled=False), stop_limit_offset_pct=1.0)
    signal = make_signal(entry=10.0, stop=9.0)
    stop_trade, _ = track_position(pm, signal, stop_order_type="STP LMT")

    pm.on_bar(FakeCtx("TEST", last_price=11.0))  # +1.0R triggers breakeven -> stop moves to entry (10.0)

    pos = pm._positions["TEST"][0]
    assert pos.current_stop_price == 10.0
    assert pos.stop_order.auxPrice == 10.0
    assert pos.stop_order.lmtPrice == 10.0 * 0.99  # 1% below the new trigger, same offset direction as entry
    assert stop_trade.order in ib.cancelled


def test_trailing_updates_stop_limit_price_when_order_is_stop_limit():
    ib = FakeIB()
    pm = PositionManager(ib, FakeJournal(), make_exits_config(trailing_method="ema"), stop_limit_offset_pct=1.0)
    signal = make_signal(entry=10.0, stop=9.0)
    stop_trade, _ = track_position(pm, signal, stop_order_type="STP LMT")

    pm.on_bar(FakeCtx("TEST", last_price=12.0, ema_9=11.0))  # breakeven then trailing ratchets to 11.0

    pos = pm._positions["TEST"][0]
    assert pos.current_stop_price == 11.0
    assert pos.stop_order.auxPrice == 11.0
    assert pos.stop_order.lmtPrice == 11.0 * 0.99


def test_reversal_exit_on_lower_low_after_breakeven_triggers_market_exit():
    ib = FakeIB()
    pm = PositionManager(ib, FakeJournal(), make_exits_config(reversal_exit_enabled=True, trailing_enabled=False))
    signal = make_signal(entry=10.0, stop=9.0)
    stop_trade, target_trade = track_position(pm, signal, quantity=100)

    # first bar: reaches breakeven (+1.0R), no reversal pattern present
    bars = make_bars([(10.0, 10.5, 9.9, 10.4, 1000), (10.4, 11.1, 10.35, 11.0, 1000)])
    pm.on_bar(FakeCtx("TEST", last_price=11.0, bars=bars))
    assert "TEST" in pm._positions
    assert pm._positions["TEST"][0].current_stop_price == 10.0  # breakeven fired

    # second bar: a lower low than the prior bar, isolated from the other
    # reversal signals (prior bar is red, not green, so red_after_green
    # doesn't also fire; body/wick ratio doesn't qualify as a topping tail)
    bars2 = make_bars([(10.4, 10.5, 10.2, 10.3, 1000), (10.3, 10.35, 10.1, 10.25, 1000)])
    pm.on_bar(FakeCtx("TEST", last_price=10.25, bars=bars2))

    market_orders = [o for _, o in ib.placed if getattr(o, "orderType", None) == "MKT"]
    assert len(market_orders) == 1
    assert "TEST" not in pm._positions


def test_open_lot_count_zero_for_untracked_symbol():
    pm = PositionManager(FakeIB(), FakeJournal(), make_exits_config())
    assert pm.open_lot_count("TEST") == 0


def test_open_lot_count_reflects_tracked_and_closed_lots():
    ib = FakeIB()
    pm = PositionManager(ib, FakeJournal(), make_exits_config())
    signal = make_signal(entry=10.0, stop=9.0)
    stop_trade, _ = track_position(pm, signal, quantity=100, target_role="target", target_qty=100)
    assert pm.open_lot_count("TEST") == 1

    stop_trade.fillEvent.emit(stop_trade, make_fill(100))
    assert pm.open_lot_count("TEST") == 0


def test_two_lots_on_same_symbol_are_tracked_and_managed_independently():
    ib = FakeIB()
    pm = PositionManager(ib, FakeJournal(), make_exits_config(trailing_enabled=False))
    first_signal = make_signal(entry=10.0, stop=9.0)  # risk_per_share = 1.0
    second_signal = make_signal(entry=11.0, stop=10.5)  # risk_per_share = 0.5, added later at a worse price
    first_stop, _ = track_position(pm, first_signal, quantity=100, target_qty=100)
    second_stop, _ = track_position(pm, second_signal, quantity=50, target_qty=50, order_id_offset=10)

    assert pm.open_lot_count("TEST") == 2

    # 11.5 is +1.5R for the first lot (entry 10, stop 9, risk_per_share 1.0)
    # and exactly +1.0R for the second lot (entry 11, stop 10.5,
    # risk_per_share 0.5) -- both cross their own breakeven trigger on the
    # same bar, independently, moving each stop to its own entry price.
    pm.on_bar(FakeCtx("TEST", last_price=11.5))

    lots = pm._positions["TEST"]
    first_lot = next(p for p in lots if p.signal is first_signal)
    second_lot = next(p for p in lots if p.signal is second_signal)
    assert first_lot.current_stop_price == 10.0
    assert second_lot.current_stop_price == 11.0
    assert pm.open_lot_count("TEST") == 2

    # Each breakeven move cancelled-and-replaced its stop -- the fill
    # listener now lives on the fresh Trade PositionManager created, not
    # the original one this test holds a reference to.
    first_stop_now = find_trade(ib, first_lot.stop_order)
    second_stop_now = find_trade(ib, second_lot.stop_order)

    first_stop_now.fillEvent.emit(first_stop_now, make_fill(100))
    assert pm.open_lot_count("TEST") == 1  # only the first lot closed
    second_stop_now.fillEvent.emit(second_stop_now, make_fill(50))
    assert pm.open_lot_count("TEST") == 0


def test_reversal_exit_lower_low_does_not_fire_before_breakeven():
    ib = FakeIB()
    pm = PositionManager(ib, FakeJournal(), make_exits_config(reversal_exit_enabled=True, trailing_enabled=False))
    signal = make_signal(entry=10.0, stop=9.0)
    track_position(pm, signal, quantity=100)

    # a lower low exists, but price hasn't reached breakeven (+1.0R) yet --
    # prior bar is red (not green), so red_after_green doesn't mask the result
    bars = make_bars([(10.3, 10.35, 9.95, 10.2, 1000), (10.2, 10.25, 9.8, 9.9, 1000)])
    pm.on_bar(FakeCtx("TEST", last_price=9.9, bars=bars))

    market_orders = [o for _, o in ib.placed if getattr(o, "orderType", None) == "MKT"]
    assert len(market_orders) == 0
    assert "TEST" in pm._positions


def test_breakeven_replace_journals_new_order_and_cancels_old_row():
    ib = FakeIB()
    journal = FakeJournal()
    pm = PositionManager(ib, journal, make_exits_config(trailing_enabled=False))
    signal = make_signal(entry=10.0, stop=9.0)
    # entry_filled=True already journals one replacement (on_entry_fill,
    # cancelling the original stop_row_id=1); breakeven below journals a
    # second, distinct one.
    track_position(pm, signal, signal_id=42)
    assert len(journal.orders_recorded) == 1
    assert (1, "Cancelled") in journal.order_statuses

    pm.on_bar(FakeCtx("TEST", last_price=11.0))  # triggers breakeven

    assert len(journal.orders_recorded) == 2
    recorded = journal.orders_recorded[-1]
    assert recorded["signal_id"] == 42
    assert recorded["role"] == "stop"
    assert recorded["stop_price"] == 10.0


def test_breakeven_replace_relinks_oca_for_single_target_case():
    ib = FakeIB()
    pm = PositionManager(ib, FakeJournal(), make_exits_config(trailing_enabled=False))
    signal = make_signal(entry=10.0, stop=9.0)
    track_position(pm, signal, target_role="target")

    pm.on_bar(FakeCtx("TEST", last_price=11.0))

    new_stop = pm._positions["TEST"][0].stop_order
    assert new_stop.ocaGroup == "TEST-0-OCA"  # matches the target's ocaGroup set by track_position


def test_breakeven_replace_does_not_oca_link_scale_out_tiers():
    ib = FakeIB()
    pm = PositionManager(ib, FakeJournal(), make_exits_config(trailing_enabled=False))
    signal = make_signal(entry=10.0, stop=9.0)
    track_position(pm, signal, target_role="scale_out", target_qty=40)

    pm.on_bar(FakeCtx("TEST", last_price=11.0))

    new_stop = pm._positions["TEST"][0].stop_order
    assert new_stop.ocaGroup == ""  # scale_out tiers are never OCA'd with the stop


def test_trailing_immune_to_external_auxprice_corruption_on_stale_order():
    # Simulates ib_async's own openOrder callback overwriting the ORIGINAL
    # (now-cancelled) order's auxPrice in place after a broker broadcast --
    # PositionManager must keep reading current_stop_price, not the stale
    # object, so this corruption has no effect on trailing decisions.
    ib = FakeIB()
    pm = PositionManager(ib, FakeJournal(), make_exits_config(trailing_method="ema"))
    signal = make_signal(entry=10.0, stop=9.0)
    stop_trade, _ = track_position(pm, signal)

    pm.on_bar(FakeCtx("TEST", last_price=11.0, ema_9=10.5))  # breakeven + trailing -> stop replaced to 10.5
    assert pm._positions["TEST"][0].current_stop_price == 10.5

    stop_trade.order.auxPrice = 999.0  # corrupt the stale, cancelled order

    pm.on_bar(FakeCtx("TEST", last_price=12.0, ema_9=11.0))
    assert pm._positions["TEST"][0].current_stop_price == 11.0  # unaffected by the corrupted stale object

def test_breakeven_does_not_fire_before_entry_has_filled():
    ib = FakeIB()
    pm = PositionManager(ib, FakeJournal(), make_exits_config(trailing_enabled=False))
    signal = make_signal(entry=10.0, stop=9.0)  # risk_per_share = 1.0
    stop_trade, _ = track_position(pm, signal, entry_filled=False)

    pm.on_bar(FakeCtx("TEST", last_price=11.0))  # would be +1.0R if the entry had filled

    assert pm._positions["TEST"][0].current_stop_price == 9.0
    assert stop_trade.order not in ib.cancelled
    assert pm._positions["TEST"][0].breakeven_done is False


def test_trailing_does_not_fire_before_entry_has_filled():
    ib = FakeIB()
    pm = PositionManager(ib, FakeJournal(), make_exits_config(trailing_method="ema"))
    signal = make_signal(entry=10.0, stop=9.0)
    stop_trade, _ = track_position(pm, signal, entry_filled=False)

    pm.on_bar(FakeCtx("TEST", last_price=11.0, ema_9=10.5))

    assert pm._positions["TEST"][0].current_stop_price == 9.0
    assert stop_trade.order not in ib.cancelled


def test_reversal_exit_does_not_fire_before_entry_has_filled():
    ib = FakeIB()
    pm = PositionManager(ib, FakeJournal(), make_exits_config(reversal_exit_enabled=True))
    signal = make_signal(entry=10.0, stop=9.0)
    track_position(pm, signal, quantity=100, entry_filled=False)

    bars = make_bars(
        [
            (10.0, 10.5, 9.9, 10.4, 1000),
            (10.4, 11.0, 10.35, 10.45, 1000),  # topping tail -- would fire if the entry had filled
        ]
    )
    pm.on_bar(FakeCtx("TEST", last_price=10.45, bars=bars))

    market_orders = [o for _, o in ib.placed if getattr(o, "orderType", None) == "MKT"]
    assert len(market_orders) == 0
    assert "TEST" in pm._positions


def test_management_resumes_once_entry_fill_arrives():
    ib = FakeIB()
    pm = PositionManager(ib, FakeJournal(), make_exits_config(trailing_enabled=False))
    signal = make_signal(entry=10.0, stop=9.0)
    parent_trade = FakeTrade(FakeOrder("BUY", 100, lmtPrice=signal.entry_price, orderId=1))
    stop_trade = FakeTrade(FakeOrder("SELL", 100, auxPrice=signal.stop_price, orderId=2, parentId=1))
    target_trade = FakeTrade(FakeOrder("SELL", 100, lmtPrice=signal.target_price, orderId=3))
    pm.track(
        contract=object(),
        signal=signal,
        signal_id=1,
        parent_trade=parent_trade,
        stop_trade=stop_trade,
        stop_row_id=1,
        target_trades=[target_trade],
        target_roles=["target"],
    )

    pm.on_bar(FakeCtx("TEST", last_price=11.0))
    assert pm._positions["TEST"][0].current_stop_price == 9.0  # gated, no entry fill yet

    pos = pm._positions["TEST"][0]
    # the entry fill arrives later, same as a real limit order finally filling
    parent_trade.fillEvent.emit(parent_trade, make_fill(100))
    assert pos.entry_filled is True

    pm.on_bar(FakeCtx("TEST", last_price=11.0))
    assert pm._positions["TEST"][0].current_stop_price == 10.0  # breakeven now applies
    assert stop_trade.order in ib.cancelled


def test_replacement_stop_does_not_carry_parent_id():
    # _replace_stop_order only ever runs once entry_filled is true (see
    # on_bar's gate), so by the time a replacement is built, the entry
    # order it would reference is already Filled and no longer open at
    # the broker. A parentId referencing it makes IBKR reject the whole
    # submission with "Can't find order with id = <parent>" (confirmed
    # live against RAMZ on 2026-09-14 -- every breakeven/trailing move
    # after the first failed validation, silently leaving the position
    # with no resting stop at all). entry_filled is the real safety net
    # against replacing protection before an entry exists, so the
    # replacement order must not set parentId at all.
    ib = FakeIB()
    pm = PositionManager(ib, FakeJournal(), make_exits_config(trailing_enabled=False))
    signal = make_signal(entry=10.0, stop=9.0)
    stop_trade, _ = track_position(pm, signal)
    assert stop_trade.order.parentId != 0  # the original bracket leg is correctly parented

    pm.on_bar(FakeCtx("TEST", last_price=11.0))  # triggers breakeven -> cancel-and-replace

    new_stop = pm._positions["TEST"][0].stop_order
    assert new_stop is not stop_trade.order  # genuinely replaced, not modified in place
    assert new_stop.parentId == 0


def test_replacement_stop_fill_is_journaled():
    ib = FakeIB()
    journal = FakeJournal()
    pm = PositionManager(ib, journal, make_exits_config(trailing_enabled=False))
    signal = make_signal(entry=10.0, stop=9.0)
    track_position(pm, signal)

    pm.on_bar(FakeCtx("TEST", last_price=11.0))  # cancel-and-replace fires
    new_stop_trade = find_trade(ib, pm._positions["TEST"][0].stop_order)

    new_stop_trade.fillEvent.emit(new_stop_trade, make_fill(100, price=10.0))

    assert len(journal.fills_recorded) == 1
    recorded = journal.fills_recorded[0]
    assert recorded["fill_qty"] == 100
    assert recorded["fill_price"] == 10.0
    assert recorded["ib_order_id"] == new_stop_trade.order.orderId


def test_original_stop_fill_is_not_double_journaled():
    # Regression (2026-09-14, live): track() wired _wire_stop_fill onto the
    # *original* bracket's stop_trade with no way to distinguish it from a
    # replacement. But OrderManager.submit_signal already runs that same
    # Trade through _attach_tracking (journaling its fills) before ever
    # calling track() -- so every fill on a stop that was never replaced
    # was recorded twice in data/journal.sqlite3. PositionManager must stay
    # silent on the original stop's fills; only a cancel-and-replace
    # revision (see test_replacement_stop_fill_is_journaled above) is its
    # job to journal, since OrderManager never sees those.
    ib = FakeIB()
    journal = FakeJournal()
    pm = PositionManager(ib, journal, make_exits_config(trailing_enabled=False))
    signal = make_signal(entry=10.0, stop=9.0)
    stop_trade, _ = track_position(pm, signal)

    stop_trade.fillEvent.emit(stop_trade, make_fill(100, price=9.0))

    assert journal.fills_recorded == []


def test_replacement_stop_sized_to_actual_fills_not_full_order_size():
    # Confirmed live incident (2026-09-14): GVH's parent entry order (3943
    # shares) was still only partially filled when breakeven fired. The
    # replacement stop was built from the *original* stop order's
    # totalQuantity (the full intended 3943), not from shares actually
    # held -- when it triggered, it sold far more than the account owned,
    # taking the position to -1441 (a naked short). remaining_qty must
    # reflect only real fills, and the replacement must be sized off it.
    ib = FakeIB()
    pm = PositionManager(ib, FakeJournal(), make_exits_config(trailing_enabled=False))
    signal = make_signal(entry=10.0, stop=9.0)
    quantity = 1000
    parent_order = FakeOrder("BUY", quantity, lmtPrice=signal.entry_price, orderId=1)
    parent_trade = FakeTrade(parent_order)
    stop_order = FakeOrder("SELL", quantity, auxPrice=signal.stop_price, orderId=2, parentId=1)
    stop_trade = FakeTrade(stop_order)
    target_order = FakeOrder("SELL", quantity, lmtPrice=signal.target_price, orderId=3)
    target_trade = FakeTrade(target_order)
    pm.track(
        contract=object(),
        signal=signal,
        signal_id=1,
        parent_trade=parent_trade,
        stop_trade=stop_trade,
        stop_row_id=1,
        target_trades=[target_trade],
        target_roles=["target"],
    )

    # Only a quarter of the intended order has actually filled so far --
    # the rest is still resting, exactly like GVH's slow, fragmented entry.
    parent_trade.fillEvent.emit(parent_trade, make_fill(250))
    assert pm._positions["TEST"][0].remaining_qty == 250

    pm.on_bar(FakeCtx("TEST", last_price=11.0))  # triggers breakeven -> cancel-and-replace

    new_stop = pm._positions["TEST"][0].stop_order
    assert new_stop.totalQuantity == 250  # sized to actual holdings, not the full 1000


def test_remaining_qty_grows_with_each_partial_entry_fill():
    ib = FakeIB()
    pm = PositionManager(ib, FakeJournal(), make_exits_config(trailing_enabled=False))
    signal = make_signal(entry=10.0, stop=9.0)
    quantity = 1000
    parent_order = FakeOrder("BUY", quantity, lmtPrice=signal.entry_price, orderId=1)
    parent_trade = FakeTrade(parent_order)
    stop_order = FakeOrder("SELL", quantity, auxPrice=signal.stop_price, orderId=2, parentId=1)
    stop_trade = FakeTrade(stop_order)
    target_order = FakeOrder("SELL", quantity, lmtPrice=signal.target_price, orderId=3)
    target_trade = FakeTrade(target_order)
    pm.track(
        contract=object(),
        signal=signal,
        signal_id=1,
        parent_trade=parent_trade,
        stop_trade=stop_trade,
        stop_row_id=1,
        target_trades=[target_trade],
        target_roles=["target"],
    )

    parent_trade.fillEvent.emit(parent_trade, make_fill(300))
    assert pm._positions["TEST"][0].remaining_qty == 300
    parent_trade.fillEvent.emit(parent_trade, make_fill(400))
    assert pm._positions["TEST"][0].remaining_qty == 700
    # the resting stop is kept in step with fills as they arrive -- resized
    # once the debounced burst settles, so the *current* stop_order is a
    # fresh object, not the original fixture's stop_order.
    flush_resize(pm._positions["TEST"][0])
    assert pm._positions["TEST"][0].stop_order.totalQuantity == 700


def test_position_stays_tracked_when_flat_but_parent_still_filling():
    # Confirmed live incident (2026-09-14): BLSG's parent order (1059
    # shares intended) had only filled 100 so far when a fast tier fill
    # sold exactly those 100, hitting remaining_qty <= 0. The old
    # _close_out unconditionally untracked the position there -- but the
    # parent kept working and filled 959 more shares later, which then had
    # no listener left to protect them at all (no resting stop, ever).
    ib = FakeIB()
    pm = PositionManager(ib, FakeJournal(), make_exits_config(trailing_enabled=False))
    signal = make_signal(entry=10.0, stop=9.0)
    parent_order = FakeOrder("BUY", 1000, lmtPrice=signal.entry_price, orderId=1)
    parent_trade = FakeTrade(parent_order, remaining=900)  # 100 of 1000 filled so far
    stop_order = FakeOrder("SELL", 1000, auxPrice=signal.stop_price, orderId=2, parentId=1)
    stop_trade = FakeTrade(stop_order)
    target_order = FakeOrder("SELL", 1000, lmtPrice=signal.target_price, orderId=3)
    target_trade = FakeTrade(target_order)
    pm.track(
        contract=object(),
        signal=signal,
        signal_id=1,
        parent_trade=parent_trade,
        stop_trade=stop_trade,
        stop_row_id=1,
        target_trades=[target_trade],
        target_roles=["scale_out"],
    )
    parent_trade.fillEvent.emit(parent_trade, make_fill(100))  # first 100 of 1000
    assert pm._positions["TEST"][0].remaining_qty == 100
    assert pm._positions["TEST"][0].parent_done is False

    # A tier fills exactly those 100 shares -- flat for the moment.
    target_trade.fillEvent.emit(target_trade, make_fill(100))

    # Must still be tracked: the parent isn't done, so this isn't really over.
    assert "TEST" in pm._positions
    pos = pm._positions["TEST"][0]
    assert pos.remaining_qty == 0

    # The parent goes on to fill the rest, much later.
    parent_trade.orderStatus.remaining = 0  # now fully filled
    parent_trade.fillEvent.emit(parent_trade, make_fill(900))

    pos = pm._positions["TEST"][0]
    assert pos.remaining_qty == 900
    assert pos.parent_done is True
    # A fresh stop now exists, correctly sized for the newly-filled shares.
    flush_resize(pos)
    assert pos.stop_order.totalQuantity == 900
    assert pos.stop_order not in ib.cancelled


def test_on_bar_skips_flat_lot_still_tracked_for_pending_parent_fills():
    ib = FakeIB()
    pm = PositionManager(ib, FakeJournal(), make_exits_config(reversal_exit_enabled=True))
    signal = make_signal(entry=10.0, stop=9.0)
    parent_order = FakeOrder("BUY", 1000, lmtPrice=signal.entry_price, orderId=1)
    parent_trade = FakeTrade(parent_order, remaining=900)
    stop_order = FakeOrder("SELL", 1000, auxPrice=signal.stop_price, orderId=2, parentId=1)
    stop_trade = FakeTrade(stop_order)
    target_order = FakeOrder("SELL", 1000, lmtPrice=signal.target_price, orderId=3)
    target_trade = FakeTrade(target_order)
    pm.track(
        contract=object(),
        signal=signal,
        signal_id=1,
        parent_trade=parent_trade,
        stop_trade=stop_trade,
        stop_row_id=1,
        target_trades=[target_trade],
        target_roles=["scale_out"],
    )
    parent_trade.fillEvent.emit(parent_trade, make_fill(100))
    target_trade.fillEvent.emit(target_trade, make_fill(100))  # flat for now

    placed_before = len(ib.placed)
    bars = make_bars([(10.0, 10.5, 9.9, 10.4, 1000), (10.4, 11.0, 10.35, 10.45, 1000)])  # topping tail shape
    pm.on_bar(FakeCtx("TEST", last_price=10.45, bars=bars))  # would trigger reversal exit if not skipped

    assert len(ib.placed) == placed_before  # no naked market order for 0 shares
    assert "TEST" in pm._positions
