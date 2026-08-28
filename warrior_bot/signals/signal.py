from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from warrior_bot.utils.rounding import round_to_tick


@dataclass
class Signal:
    symbol: str
    strategy: str
    side: str            # "BUY" (long-only for these setups)
    entry_price: float
    stop_price: float
    target_price: float
    ts: datetime
    context: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Every price on this Signal eventually reaches IBKR (directly or
        # via bracket_builder's offset math) -- round here once so no
        # order is ever rejected with error 110 ("price does not conform
        # to the minimum price variation").
        self.entry_price = round_to_tick(self.entry_price)
        self.stop_price = round_to_tick(self.stop_price)
        self.target_price = round_to_tick(self.target_price)

    @property
    def risk_per_share(self) -> float:
        return abs(self.entry_price - self.stop_price)
