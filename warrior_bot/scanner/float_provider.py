from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger("warrior_bot.scanner.float_provider")


@dataclass
class FloatRow:
    symbol: str
    float_shares: float
    updated_at: datetime


class FloatProvider:
    """Optional, best-effort float lookup.

    IBKR does not reliably expose share float. This reads a manually
    maintained CSV (symbol, float_shares, updated_at) rather than scraping
    a third party. If float filtering is enabled but a symbol is missing or
    its row is stale, the filter is SKIPPED for that symbol (treated as
    "unknown, don't block") rather than rejecting it — float filtering
    degrades gracefully to off, it never silently misbehaves.
    """

    def __init__(self, csv_path: Path, max_age_days: int = 30):
        self.csv_path = csv_path
        self.max_age_days = max_age_days
        self._rows: dict[str, FloatRow] = {}
        self._loaded = False

    def _load(self) -> None:
        self._rows = {}
        if not self.csv_path.exists():
            logger.info("Float list not found at %s — float filtering will skip all symbols", self.csv_path)
            self._loaded = True
            return
        with open(self.csv_path, "r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    self._rows[row["symbol"].upper()] = FloatRow(
                        symbol=row["symbol"].upper(),
                        float_shares=float(row["float_shares"]),
                        updated_at=datetime.fromisoformat(row["updated_at"]),
                    )
                except (KeyError, ValueError) as exc:
                    logger.warning("Skipping malformed float_list.csv row %r: %s", row, exc)
        self._loaded = True

    def passes_filter(self, symbol: str, max_float_shares: float) -> bool:
        """True if the symbol should be allowed through. Unknown/stale data always passes."""
        if not self._loaded:
            self._load()
        row = self._rows.get(symbol.upper())
        if row is None:
            return True
        if datetime.now() - row.updated_at > timedelta(days=self.max_age_days):
            return True
        return row.float_shares <= max_float_shares

    def get_float_shares(self, symbol: str) -> float | None:
        """Fresh float share count for `symbol`, or None if unknown/stale --
        same "unknown degrades gracefully" contract as passes_filter."""
        if not self._loaded:
            self._load()
        row = self._rows.get(symbol.upper())
        if row is None:
            return None
        if datetime.now() - row.updated_at > timedelta(days=self.max_age_days):
            return None
        return row.float_shares
