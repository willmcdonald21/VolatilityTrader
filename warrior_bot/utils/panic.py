from __future__ import annotations

import logging

from ib_async import IB, MarketOrder

from warrior_bot.logging_setup import alert

logger = logging.getLogger("warrior_bot.utils.panic")


def cancel_all_orders(ib: IB, channel: str = "kill_switch") -> None:
    """Cancel every active order on the account, including ones this
    process didn't place itself (reqGlobalCancel is account-wide, not
    per-client)."""
    ib.reqGlobalCancel()
    alert("reqGlobalCancel issued — all active orders cancelled", channel=channel)


def flatten_all_positions(ib: IB, channel: str = "kill_switch") -> None:
    """Market-close every open position. Used by the manual kill switch
    (scripts/kill_switch.py) and by WarriorBot's automatic EOD/daily-loss
    flatten triggers -- this is intentionally the one place in the bot
    that sends a market order with no bracket, because the goal here is
    "get flat now", not "get a good price". `channel` routes the Discord
    alert to "kill_switch" (manual) or "limits" (automatic) accordingly."""
    positions = ib.positions()
    for pos in positions:
        if pos.position == 0:
            continue
        action = "SELL" if pos.position > 0 else "BUY"
        qty = abs(pos.position)
        order = MarketOrder(action, qty)
        ib.placeOrder(pos.contract, order)
        alert(f"Flattening {pos.contract.symbol} qty={qty} via market {action}", channel=channel)
    logger.warning("Flatten requested for %d position(s)", len(positions))


def panic_stop(ib: IB, flatten: bool = True, channel: str = "kill_switch") -> None:
    cancel_all_orders(ib, channel=channel)
    if flatten:
        flatten_all_positions(ib, channel=channel)
