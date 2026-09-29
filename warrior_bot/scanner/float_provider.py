from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger("warrior_bot.scanner.float_provider")


def _as_naive_local(value: datetime) -> datetime:
    """Drops tzinfo so the staleness check can compare against a naive
    datetime.now(). A tz-aware timestamp in the CSV previously raised
    TypeError ("can't subtract offset-naive and offset-aware datetimes")
    from inside passes_filter -- i.e. in the signal path."""
    return value.astimezone().replace(tzinfo=None) if value.tzinfo is not None else value


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
        # mtime of the CSV when it was last read. The file used to be
        # cached forever, so a long-running bot never picked up an edit
        # without a restart.
        self._loaded_mtime: float | None = None

    def is_available(self) -> bool:
        """True when the CSV exists and yielded at least one usable row.

        Lets startup report a float filter that is configured as enabled
        but cannot actually do anything -- config.yaml advertises
        enable_float_filter with a 'matches Ross Cameron's 5 Pillars'
        comment, yet with no config/float_list.csv on disk the filter had
        never rejected a single symbol: 0 of 2,311 signals carried any
        float data."""
        self._ensure_loaded()
        return bool(self._rows)

    def _ensure_loaded(self) -> None:
        """Loads on first use, and reloads when the CSV changes on disk."""
        try:
            mtime = self.csv_path.stat().st_mtime
        except OSError:
            mtime = None
        if self._loaded and mtime == self._loaded_mtime:
            return
        self._load()
        self._loaded_mtime = mtime

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
                        updated_at=_as_naive_local(datetime.fromisoformat(row["updated_at"])),
                    )
                except (KeyError, ValueError) as exc:
                    logger.warning("Skipping malformed float_list.csv row %r: %s", row, exc)
        self._loaded = True

    def passes_filter(self, symbol: str, max_float_shares: float) -> bool:
        """True if the symbol should be allowed through. Unknown/stale data always passes."""
        self._ensure_loaded()
        row = self._rows.get(symbol.upper())
        if row is None:
            return True
        if datetime.now() - row.updated_at > timedelta(days=self.max_age_days):
            return True
        return row.float_shares <= max_float_shares

    def get_float_shares(self, symbol: str) -> float | None:
        """Fresh float share count for `symbol`, or None if unknown/stale --
        same "unknown degrades gracefully" contract as passes_filter."""
        self._ensure_loaded()
        row = self._rows.get(symbol.upper())
        if row is None:
            return None
        if datetime.now() - row.updated_at > timedelta(days=self.max_age_days):
            return None
        return row.float_shares
