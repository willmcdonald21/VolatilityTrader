from __future__ import annotations

import copy
import logging
from datetime import datetime, time, timezone
from typing import Callable

from ib_async import IB, LimitOrder, MarketOrder, Order, Trade

from warrior_bot.logging_setup import alert
from warrior_bot.utils.rounding import round_to_tick
from warrior_bot.utils.time_utils import to_eastern

# Called (symbol, Trade, Order) right after an emergency-flatten order is
# placed, so a caller that has a Journal/PositionManager (main.py) can
# attach fill journaling -- this module deliberately stays free of those
# dependencies itself (see flatten_position's docstring for why that
# matters: without a listener here, an emergency-flatten fill was
# invisible to the journal/dashboard entirely, confirmed live 2026-09-23
# while investigating a run of losing days -- any position force-closed by
# the reconciliation watchdog or routine EOD flatten showed as "still
# open, $0 realized" no matter what it actually closed at).
OnOrderPlaced = Callable[[str, Trade, Order], None]

logger = logging.getLogger("warrior_bot.utils.panic")


def cancel_all_orders(ib: IB, channel: str = "kill_switch") -> None:
    """Cancel every active order on the account, including ones this
    process didn't place itself (reqGlobalCancel is account-wide, not
    per-client)."""
    ib.reqGlobalCancel()
    alert("reqGlobalCancel issued — all active orders cancelled", channel=channel)


FLATTEN_ORDER_REF = "warrior_flatten"
RTH_OPEN = time(9, 30)
RTH_CLOSE = time(16, 0)


def _in_regular_hours(now: datetime | None = None) -> bool:
    now_et = to_eastern(now or datetime.now(timezone.utc))
    return now_et.weekday() < 5 and RTH_OPEN <= now_et.time() < RTH_CLOSE


def _pending_flatten_qty(ib: IB, symbol: str) -> float:
    """Shares already covered by a still-working flatten order for `symbol`.
    Only orders this module placed count (tagged via orderRef) -- a resting
    take-profit or stop is not a flatten."""
    pending = 0.0
    for trade in ib.openTrades():
        if trade.contract.symbol != symbol or trade.order.orderRef != FLATTEN_ORDER_REF:
            continue
        pending += trade.orderStatus.remaining or trade.order.totalQuantity
    return pending


def _last_price(ib: IB, symbol: str) -> float | None:
    for item in ib.portfolio():
        if item.contract.symbol == symbol:
            price = item.marketPrice
            if price is not None and price == price and price > 0:  # price == price: not NaN
                return float(price)
    return None


def flatten_position(
    ib: IB,
    position,
    channel: str = "kill_switch",
    limit_offset_pct: float = 5.0,
    skip_if_pending: bool = True,
    now: datetime | None = None,
    on_order_placed: OnOrderPlaced | None = None,
) -> bool:
    """Closes a single position (one element of ib.positions()); returns
    True if an order was placed, False if there was nothing to do.
    Factored out of flatten_all_positions so a caller that has already
    identified exactly one symbol needing to get flat immediately (e.g.
    main.py's position-reconciliation watchdog finding a real position
    with no resting protective stop) doesn't have to route through -- and
    risk touching -- every other open position via reqGlobalCancel/a full
    account-wide flatten.

    Regular hours: a market order. Outside them: a marketable LIMIT
    `limit_offset_pct` through the last price. IBKR ignores outsideRth on a
    market order ("Attribute 'Outside Regular Trading Hours' is ignored",
    code 2109) and just queues it until 09:30 -- on 2026-09-21 the daily
    loss limit fired at 04:18 ET and the flatten did not execute for five
    hours, with the stops already cancelled.

    Idempotent: with skip_if_pending, a symbol that already has a working
    flatten order covering its whole position is left alone. The
    reconciliation watchdog re-checks every 30s; without this each pass
    queued another sell, all of which filled at the open and turned a long
    into a naked short."""
    if position.position == 0:
        return False
    symbol = position.contract.symbol
    action = "SELL" if position.position > 0 else "BUY"
    qty = abs(position.position)
    if skip_if_pending and _pending_flatten_qty(ib, symbol) >= qty:
        logger.info("Flatten for %s already working (%s shares) -- not re-sending", symbol, qty)
        return False

    price = None if _in_regular_hours(now) else _last_price(ib, symbol)
    if price is not None:
        offset = limit_offset_pct / 100.0
        limit = price * (1 - offset) if action == "SELL" else price * (1 + offset)
        order = LimitOrder(action, qty, round_to_tick(limit), outsideRth=True, tif="DAY", orderRef=FLATTEN_ORDER_REF)
        kind = f"marketable limit {order.lmtPrice}"
    else:
        # In RTH a market order fills; outside it with no usable price there
        # is nothing better to build a limit from -- this queues until the
        # open, so say so.
        order = MarketOrder(action, qty, outsideRth=True, tif="DAY", orderRef=FLATTEN_ORDER_REF)
        kind = "market"
        if not _in_regular_hours(now):
            logger.warning("No usable last price for %s -- flatten will queue until the open", symbol)
    # ib.positions() reports each position's actual trading exchange
    # (e.g. NASDAQ) rather than SMART -- routing an order directly to it
    # triggers IBKR's precautionary direct-routing rejection (error
    # 201/10311, hit during the Aug 27 BIRD/BMRA flatten attempt). Route
    # through SMART instead, on a copy so the shared Contract object from
    # positions() isn't mutated.
    contract = copy.copy(position.contract)
    contract.exchange = "SMART"
    trade = ib.placeOrder(contract, order)
    alert(f"Flattening {symbol} qty={qty} via {kind} {action}", channel=channel)
    if on_order_placed is not None:
        on_order_placed(symbol, trade, order)
    return True


def flatten_all_positions(
    ib: IB, channel: str = "kill_switch", limit_offset_pct: float = 5.0, on_order_placed: OnOrderPlaced | None = None
) -> None:
    """Close every open position. Used by the manual kill switch
    (scripts/kill_switch.py) and by WarriorBot's automatic EOD/daily-loss
    flatten triggers -- intentionally the one place in the bot that sends
    an unbracketed closing order, because the goal here is "get flat now",
    not "get a good price". `channel` routes the Discord alert to
    "kill_switch" (manual) or "limits" (automatic) accordingly.

    skip_if_pending is off: panic_stop has just cancelled everything, so any
    flatten order still listed by openTrades() is one that is being
    cancelled and must be replaced, not respected."""
    positions = ib.positions()
    for pos in positions:
        flatten_position(
            ib,
            pos,
            channel=channel,
            limit_offset_pct=limit_offset_pct,
            skip_if_pending=False,
            on_order_placed=on_order_placed,
        )
    logger.warning("Flatten requested for %d position(s)", len(positions))


def panic_stop(
    ib: IB,
    flatten: bool = True,
    channel: str = "kill_switch",
    limit_offset_pct: float = 5.0,
    on_order_placed: OnOrderPlaced | None = None,
) -> None:
    cancel_all_orders(ib, channel=channel)
    if flatten:
        flatten_all_positions(ib, channel=channel, limit_offset_pct=limit_offset_pct, on_order_placed=on_order_placed)
