from __future__ import annotations

from dataclasses import dataclass

from ib_async import IB, LimitOrder, Order, StopOrder

from warrior_bot.signals.signal import Signal


@dataclass
class Bracket:
    parent: Order
    take_profit: Order
    stop_loss: Order

    @property
    def orders(self) -> list[Order]:
        return [self.parent, self.take_profit, self.stop_loss]


def build_bracket(ib: IB, signal: Signal, quantity: int) -> Bracket:
    """Every entry is a 3-order bracket — no naked entries.

    Mirrors ib_async's IB.bracketOrder() (parent + take-profit both
    transmit=False, stop-loss transmit=True as the last leg submitted) but
    additionally OCA-links the two exit legs, which bracketOrder() does
    NOT do by itself — verified by reading the installed ib_async source
    (site-packages/ib_async/ib.py::bracketOrder), not assumed.
    """
    reverse_action = "SELL" if signal.side == "BUY" else "BUY"

    parent = LimitOrder(
        signal.side,
        quantity,
        signal.entry_price,
        orderId=ib.client.getReqId(),
        transmit=False,
    )
    take_profit = LimitOrder(
        reverse_action,
        quantity,
        signal.target_price,
        orderId=ib.client.getReqId(),
        parentId=parent.orderId,
        transmit=False,
    )
    stop_loss = StopOrder(
        reverse_action,
        quantity,
        signal.stop_price,
        orderId=ib.client.getReqId(),
        parentId=parent.orderId,
        transmit=True,
    )

    oca_group = f"{signal.symbol}-{parent.orderId}-OCA"
    IB.oneCancelsAll([take_profit, stop_loss], oca_group, ocaType=1)

    return Bracket(parent=parent, take_profit=take_profit, stop_loss=stop_loss)
