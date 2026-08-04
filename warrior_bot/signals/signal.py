from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


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

    @property
    def risk_per_share(self) -> float:
        return abs(self.entry_price - self.stop_price)
