from __future__ import annotations

from dataclasses import dataclass

from ib_async import IB, LimitOrder, Order, StopLimitOrder

from warrior_bot.signals.signal import Signal


@dataclass
class Bracket:
    parent: Order
    take_profits: list[Order]
    stop_loss: Order
    # Parallel to take_profits: "target" (single, full-qty, OCA'd with the
    # stop) | "scale_out" (one of several partial legs, independent of the
    # stop -- see build_bracket docstring for why those can't be OCA'd).
    target_roles: list[str]

    @property
    def orders(self) -> list[Order]:
        return [self.parent, *self.take_profits, self.stop_loss]


def build_bracket(
    ib: IB,
    signal: Signal,
    quantity: int,
    profit_tiers: list[tuple[int, float]] | None = None,
    stop_limit_offset_pct: float = 0.5,
) -> Bracket:
    """Every entry is a bracket — no naked entries.

    Mirrors ib_async's IB.bracketOrder() (parent + take-profit(s) all
    transmit=False, stop-loss transmit=True as the last leg submitted) but
    additionally OCA-links a single full-quantity target with the stop,
    which bracketOrder() does NOT do by itself — verified by reading the
    installed ib_async source (site-packages/ib_async/ib.py::bracketOrder),
    not assumed.

    `profit_tiers` is a list of (qty, price) partial take-profit legs
    (e.g. from ExitsConfig.profit_tiers x signal.risk_per_share), each
    built as its own independent resting limit order and deliberately NOT
    OCA-linked to the stop: OCA type 1 cancels the *other* order outright
    on a fill, which would cancel the full-quantity stop the instant the
    first, smaller tier fills, leaving the remaining shares unprotected.
    Instead `PositionManager` reacts to each tier's fill and resizes the
    stop down. When `profit_tiers` is omitted/empty, falls back to a
    single full-quantity target at signal.target_price, OCA'd with the
    stop as before (mutually exclusive full fills, safe to auto-cancel the
    counterpart).

    The stop-loss is a stop-limit (STP LMT), not a plain stop -- IBKR
    rejects plain market orders (which is what a triggered STP order
    resolves to) outside regular trading hours, and this bot trades
    pre-market. `stop_limit_offset_pct` sits the limit this % beyond the
    stop trigger (in the direction that still allows the exit to fill),
    capping worst-case slippage the same way a manual trader's marketable
    limit order would.
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

    valid_tiers = [(qty, price) for qty, price in (profit_tiers or []) if 0 < qty <= quantity]
    use_tiers = bool(valid_tiers)
    if use_tiers:
        tier_specs = valid_tiers
        target_roles = ["scale_out"] * len(tier_specs)
    else:
        tier_specs = [(quantity, signal.target_price)]
        target_roles = ["target"]

    take_profits = [
        LimitOrder(
            reverse_action,
            exit_qty,
            exit_price,
            orderId=ib.client.getReqId(),
            parentId=parent.orderId,
            transmit=False,
            outsideRth=True,
            tif="DAY",
        )
        for exit_qty, exit_price in tier_specs
    ]

    # Limit sits on the far side of the trigger from the stop's protective
    # direction: a SELL stop (protecting a long) triggers as price falls,
    # so the limit sits below the trigger to still be fillable on further
    # downside; a BUY stop (protecting a short) is the mirror image.
    if reverse_action == "SELL":
        stop_limit_price = signal.stop_price * (1 - stop_limit_offset_pct / 100.0)
    else:
        stop_limit_price = signal.stop_price * (1 + stop_limit_offset_pct / 100.0)

    stop_loss = StopLimitOrder(
        reverse_action,
        quantity,
        lmtPrice=stop_limit_price,
        stopPrice=signal.stop_price,
        orderId=ib.client.getReqId(),
        parentId=parent.orderId,
        transmit=True,
        outsideRth=True,
        tif="DAY",
    )

    if not use_tiers:
        oca_group = f"{signal.symbol}-{parent.orderId}-OCA"
        IB.oneCancelsAll([take_profits[0], stop_loss], oca_group, ocaType=1)

    return Bracket(parent=parent, take_profits=take_profits, stop_loss=stop_loss, target_roles=target_roles)
