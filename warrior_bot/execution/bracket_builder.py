from __future__ import annotations

from dataclasses import dataclass

from ib_async import IB, LimitOrder, Order, StopOrder

from warrior_bot.signals.signal import Signal


@dataclass
class Bracket:
    parent: Order
    take_profit: Order
    stop_loss: Order
    target_role: str = "target"  # "target" (static, OCA'd with stop) | "scale_out" (partial, independent)

    @property
    def orders(self) -> list[Order]:
        return [self.parent, self.take_profit, self.stop_loss]


def build_bracket(
    ib: IB,
    signal: Signal,
    quantity: int,
    scale_out_qty: int | None = None,
    scale_out_price: float | None = None,
) -> Bracket:
    """Every entry is a 3-order bracket — no naked entries.

    Mirrors ib_async's IB.bracketOrder() (parent + take-profit both
    transmit=False, stop-loss transmit=True as the last leg submitted) but
    additionally OCA-links the two exit legs, which bracketOrder() does
    NOT do by itself — verified by reading the installed ib_async source
    (site-packages/ib_async/ib.py::bracketOrder), not assumed.

    When `scale_out_qty` is given (and less than `quantity`), the exit leg
    only covers that partial quantity at `scale_out_price` and is
    deliberately NOT OCA-linked to the stop: OCA type 1 cancels the *other*
    order outright on a fill, which would cancel the full-quantity stop the
    instant the smaller scale-out leg fills, leaving the remaining shares
    unprotected. Instead `PositionManager` reacts to the scale-out fill
    itself and resizes the stop down.
    """
    reverse_action = "SELL" if signal.side == "BUY" else "BUY"

    parent = LimitOrder(
        signal.side,
        quantity,
        signal.entry_price,
        orderId=ib.client.getReqId(),
        transmit=False,
        outsideRth=True,
        tif="DAY",
    )

    use_scale_out = scale_out_qty is not None and 0 < scale_out_qty < quantity
    if use_scale_out:
        exit_qty = scale_out_qty
        exit_price = scale_out_price if scale_out_price is not None else signal.target_price
        target_role = "scale_out"
    else:
        exit_qty = quantity
        exit_price = signal.target_price
        target_role = "target"

    take_profit = LimitOrder(
        reverse_action,
        exit_qty,
        exit_price,
        orderId=ib.client.getReqId(),
        parentId=parent.orderId,
        transmit=False,
        outsideRth=True,
        tif="DAY",
    )
    stop_loss = StopOrder(
        reverse_action,
        quantity,
        signal.stop_price,
        orderId=ib.client.getReqId(),
        parentId=parent.orderId,
        transmit=True,
        outsideRth=True,
        tif="DAY",
    )

    if not use_scale_out:
        oca_group = f"{signal.symbol}-{parent.orderId}-OCA"
        IB.oneCancelsAll([take_profit, stop_loss], oca_group, ocaType=1)

    return Bracket(parent=parent, take_profit=take_profit, stop_loss=stop_loss, target_role=target_role)
