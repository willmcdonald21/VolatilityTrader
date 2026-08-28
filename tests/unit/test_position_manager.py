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
    def __init__(
        self, action, totalQuantity, auxPrice=None, lmtPrice=None, orderId=1, orderType=None, ocaGroup=""
    ):
        self.action = action
        self.totalQuantity = totalQuantity
        self.auxPrice = auxPrice
        self.lmtPrice = lmtPrice
        self.orderId = orderId
        self.orderType = orderType
        self.ocaGroup = ocaGroup


class FakeTrade:
    def __init__(self, order: FakeOrder):
        self.order = order
        self.fillEvent = FakeEvent()
        self.statusEvent = FakeEvent()
        self.orderStatus = SimpleNamespace(status="Submitted")


class FakeClient:
    def __init__(self):
        self._next_id = 100

    def getReqId(self) -> int:
        rid = self._next_id
        self._next_id += 1
        return rid


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


class FakeJournal:
    def __init__(self):
        self.price_updates = []
        self.kill_switch_events = []
        self.orders_recorded = []
        self.order_statuses = []
        self._next_row_id = 100

    def update_order_price(self, order_row_id, limit_price=None, stop_price=None, qty=None):
        self.price_updates.append((order_row_id, limit_price, stop_price, qty))

    def record_order(
        self, signal_id, ib_order_id, role, action, qty, order_type, limit_price, stop_price, oca_group, status
    ):
        row_id = self._next_row_id
        self._next_row_id += 1
        self.orders_recorded.append(
            {
                "row_id": row_id,
                "signal_id": signal_id,
                "ib_order_id": ib_order_id,
                "role": role,
                "action": action,
                "qty": qty,
                "order_type": order_type,
                "limit_price": limit_price,
                "stop_price": stop_price,
                "oca_group": oca_group,
                "status": status,
            }
        )
        return row_id

    def update_order_status(self, order_row_id, status):
        self.order_statuses.append((order_row_id, status))

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


def track_position(
    pm: PositionManager,
    signal: Signal,
    quantity=100,
    target_role="target",
    target_qty=None,
    stop_order_type=None,
    signal_id=1,
):
    stop_order = FakeOrder("SELL", quantity, auxPrice=signal.stop_price, orderId=2, orderType=stop_order_type)
    stop_trade = FakeTrade(stop_order)
    tq = target_qty if target_qty is not None else quantity
    # bracket_builder only OCA-links the target/stop pair for a static
    # target -- a scale_out leg is deliberately never OCA'd (see its own
    # docstring), so the fixture mirrors that: non-empty ocaGroup only
    # for target_role="target".
    target_oca_group = f"{signal.symbol}-OCA" if target_role == "target" else ""
    target_order = FakeOrder("SELL", tq, lmtPrice=signal.target_price, orderId=3, ocaGroup=target_oca_group)
    target_trade = FakeTrade(target_order)
    pm.track(
        contract=object(),
        signal=signal,
        signal_id=signal_id,
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
    assert pm._positions["TEST"].current_stop_price == 9.0

    pm.on_bar(FakeCtx("TEST", last_price=11.0))  # +1.0R, triggers breakeven
    assert pm._positions["TEST"].current_stop_price == 10.0
    # cancel-and-replace: the original stop_trade.order is cancelled, a
    # brand-new order object is what actually got placed
    assert stop_trade.order in ib.cancelled
    assert pm._positions["TEST"].stop_order is not stop_trade.order
    assert any(order is pm._positions["TEST"].stop_order for _, order in ib.placed)


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
    assert pm._positions["TEST"].current_stop_price == 10.0


def test_trailing_only_moves_stop_up_never_down():
    ib = FakeIB()
    pm = PositionManager(ib, FakeJournal(), make_exits_config(trailing_method="ema"))
    signal = make_signal(entry=10.0, stop=9.0)
    stop_trade, _ = track_position(pm, signal)

    # +1.0R triggers breakeven (stop -> 10.0) then, same bar, trailing
    # ratchets it further to the EMA candidate (10.5).
    pm.on_bar(FakeCtx("TEST", last_price=11.0, ema_9=10.5))
    assert pm._positions["TEST"].current_stop_price == 10.5

    # price and EMA keep rising -- stop trails up to 11.0
    pm.on_bar(FakeCtx("TEST", last_price=12.0, ema_9=11.0))
    assert pm._positions["TEST"].current_stop_price == 11.0

    # price pulls back and EMA drops below the current stop -- must NOT loosen
    pm.on_bar(FakeCtx("TEST", last_price=11.5, ema_9=10.8))
    assert pm._positions["TEST"].current_stop_price == 11.0


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
    assert pm._positions["TEST"].current_stop_price == 10.0  # breakeven still ran since no reversal fired


def test_breakeven_updates_stop_limit_price_when_order_is_stop_limit():
    ib = FakeIB()
    pm = PositionManager(ib, FakeJournal(), make_exits_config(trailing_enabled=False), stop_limit_offset_pct=1.0)
    signal = make_signal(entry=10.0, stop=9.0)
    stop_trade, _ = track_position(pm, signal, stop_order_type="STP LMT")

    pm.on_bar(FakeCtx("TEST", last_price=11.0))  # +1.0R triggers breakeven -> stop moves to entry (10.0)

    pos = pm._positions["TEST"]
    assert pos.current_stop_price == 10.0
    assert pos.stop_order.lmtPrice == 10.0 * 0.99  # 1% below the new trigger, same offset direction as entry


def test_trailing_updates_stop_limit_price_when_order_is_stop_limit():
    ib = FakeIB()
    pm = PositionManager(ib, FakeJournal(), make_exits_config(trailing_method="ema"), stop_limit_offset_pct=1.0)
    signal = make_signal(entry=10.0, stop=9.0)
    stop_trade, _ = track_position(pm, signal, stop_order_type="STP LMT")

    pm.on_bar(FakeCtx("TEST", last_price=12.0, ema_9=11.0))  # breakeven then trailing ratchets to 11.0

    pos = pm._positions["TEST"]
    assert pos.current_stop_price == 11.0
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
    assert pm._positions["TEST"].current_stop_price == 10.0  # breakeven fired

    # second bar: a lower low than the prior bar, isolated from the other
    # reversal signals (prior bar is red, not green, so red_after_green
    # doesn't also fire; body/wick ratio doesn't qualify as a topping tail)
    bars2 = make_bars([(10.4, 10.5, 10.2, 10.3, 1000), (10.3, 10.35, 10.1, 10.25, 1000)])
    pm.on_bar(FakeCtx("TEST", last_price=10.25, bars=bars2))

    market_orders = [o for _, o in ib.placed if getattr(o, "orderType", None) == "MKT"]
    assert len(market_orders) == 1
    assert "TEST" not in pm._positions


def test_trailing_replace_issues_cancel_and_new_placeOrder_not_in_place_mutation():
    ib = FakeIB()
    pm = PositionManager(ib, FakeJournal(), make_exits_config(trailing_method="ema"))
    signal = make_signal(entry=10.0, stop=9.0)
    stop_trade, _ = track_position(pm, signal)

    pm.on_bar(FakeCtx("TEST", last_price=11.0, ema_9=10.5))  # breakeven then trailing

    assert stop_trade.order in ib.cancelled
    new_order = pm._positions["TEST"].stop_order
    assert new_order is not stop_trade.order
    assert new_order.orderId != stop_trade.order.orderId
    assert any(order is new_order for _, order in ib.placed)


def test_replacement_stop_fill_closes_out_position():
    ib = FakeIB()
    pm = PositionManager(ib, FakeJournal(), make_exits_config(trailing_enabled=False))
    signal = make_signal(entry=10.0, stop=9.0)
    track_position(pm, signal, quantity=100, target_role="target", target_qty=100)

    pm.on_bar(FakeCtx("TEST", last_price=11.0))  # breakeven -> cancel-and-replace

    new_trade = ib.trades[-1]
    assert new_trade.order is pm._positions["TEST"].stop_order

    new_trade.fillEvent.emit(new_trade, make_fill(100))

    assert "TEST" not in pm._positions  # proves the REPLACEMENT stop's fill was actually wired up


def test_trailing_immune_to_external_auxprice_corruption():
    # Regression test for the corrupted-read-back bug: ib_async's openOrder
    # callback can silently overwrite pos.stop_order.auxPrice in place
    # (e.g. after IBKR's own error-399 anti-crossing repricing) with no
    # relationship to PositionManager's own trailing state. This must not
    # be able to fool the monotonic "stop only tightens" invariant, since
    # current_stop_price -- not pos.stop_order.auxPrice -- is authoritative.
    ib = FakeIB()
    pm = PositionManager(ib, FakeJournal(), make_exits_config(trailing_method="ema"))
    signal = make_signal(entry=10.0, stop=9.0)
    track_position(pm, signal)

    # +1.0R triggers breakeven (stop -> 10.0) then, same bar, trailing
    # ratchets it further to the EMA candidate (10.5) -- same as
    # test_trailing_only_moves_stop_up_never_down.
    pm.on_bar(FakeCtx("TEST", last_price=11.0, ema_9=10.5))
    assert pm._positions["TEST"].current_stop_price == 10.5
    placed_before = len(ib.placed)

    # simulate ib_async's openOrder callback corrupting the shared Order
    # object between two _check_trailing calls
    pm._positions["TEST"].stop_order.auxPrice = 9.5

    # a candidate that's a real regression from 10.5 but would look like
    # tightening if current_stop were (wrongly) read from the corrupted 9.5
    pm.on_bar(FakeCtx("TEST", last_price=11.5, ema_9=9.8))

    assert pm._positions["TEST"].current_stop_price == 10.5  # unchanged -- not fooled
    assert len(ib.placed) == placed_before  # no bogus replace was attempted


def test_breakeven_replace_relinks_oca_with_live_target():
    ib = FakeIB()
    pm = PositionManager(ib, FakeJournal(), make_exits_config(trailing_enabled=False))
    signal = make_signal(entry=10.0, stop=9.0)
    _, target_trade = track_position(pm, signal, target_role="target")

    pm.on_bar(FakeCtx("TEST", last_price=11.0))  # breakeven fires

    new_stop = pm._positions["TEST"].stop_order
    assert new_stop.ocaGroup == target_trade.order.ocaGroup
    assert new_stop.ocaGroup != ""


def test_trailing_replace_is_standalone_after_target_cancelled():
    ib = FakeIB()
    pm = PositionManager(ib, FakeJournal(), make_exits_config(trailing_method="ema"))
    signal = make_signal(entry=10.0, stop=9.0)
    track_position(pm, signal, target_role="target")

    # +1.0R triggers breakeven (OCA-relinked to the still-live target),
    # then same bar trailing activates -- cancels the target, and its own
    # replacement stop must come out standalone (no OCA group)
    pm.on_bar(FakeCtx("TEST", last_price=11.0, ema_9=10.5))

    final_stop = pm._positions["TEST"].stop_order
    assert final_stop.ocaGroup == ""


def test_scale_out_target_never_relinks_oca_on_breakeven_replace():
    ib = FakeIB()
    pm = PositionManager(ib, FakeJournal(), make_exits_config(trailing_enabled=False))
    signal = make_signal(entry=10.0, stop=9.0)
    track_position(pm, signal, target_role="scale_out", target_qty=40)

    pm.on_bar(FakeCtx("TEST", last_price=11.0))  # breakeven fires

    new_stop = pm._positions["TEST"].stop_order
    assert new_stop.ocaGroup == ""


def test_replace_journals_new_row_and_marks_old_cancelled():
    ib = FakeIB()
    journal = FakeJournal()
    pm = PositionManager(ib, journal, make_exits_config(trailing_enabled=False))
    signal = make_signal(entry=10.0, stop=9.0)
    track_position(pm, signal, signal_id=42)

    pm.on_bar(FakeCtx("TEST", last_price=11.0))  # breakeven fires -> one replace

    assert journal.order_statuses == [(1, "Cancelled")]
    assert len(journal.orders_recorded) == 1
    recorded = journal.orders_recorded[0]
    assert recorded["signal_id"] == 42
    assert recorded["role"] == "stop"
    assert recorded["stop_price"] == 10.0
    assert pm._positions["TEST"].stop_row_id == recorded["row_id"]


def test_atr_trailing_stop_price_is_tick_conformant():
    # A deliberately "dirty" ATR value (last_price - atr*atr_multiple
    # produces a many-decimal raw candidate) confirms _replace_stop_order
    # rounds both the trigger (auxPrice) and the STP LMT limit price
    # before submitting the replacement order to IBKR.
    ib = FakeIB()
    pm = PositionManager(
        ib, FakeJournal(), make_exits_config(trailing_method="atr"), stop_limit_offset_pct=1.0
    )
    signal = make_signal(entry=10.0, stop=9.0)
    stop_trade, _ = track_position(pm, signal, stop_order_type="STP LMT")

    # last_price=11.0 triggers breakeven (+1.0R) then, same bar, trailing
    # ratchets to the ATR candidate: 11.0 - 0.223456 * 1.5 = 10.664816 raw
    pm.on_bar(FakeCtx("TEST", last_price=11.0, atr_value=0.223456))

    pos = pm._positions["TEST"]
    assert pos.current_stop_price == 10.66
    assert pos.stop_order.lmtPrice == 10.55


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