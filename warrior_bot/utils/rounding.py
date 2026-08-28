from __future__ import annotations


def tick_size(price: float) -> float:
    """SEC Reg NMS Rule 612 sub-penny rule: $0.01 minimum price variation
    at/above $1.00, $0.0001 below $1.00. Static -- this bot only trades
    US NMS-listed stocks, so no per-symbol reqContractDetails/minTick
    lookup is needed."""
    return 0.01 if price >= 1.0 else 0.0001


def round_to_tick(price: float) -> float:
    """Round `price` to the nearest valid tick so IBKR doesn't reject it
    with error 110 ("price does not conform to the minimum price
    variation"). A single round(price/tick)*tick can leave floating-point
    residue (e.g. 13.279999999998), so the result is rounded a second
    time to the tick's own decimal precision to guarantee a clean value."""
    tick = tick_size(price)
    decimals = 2 if tick == 0.01 else 4
    return round(round(price / tick) * tick, decimals)
