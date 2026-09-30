"""One trade reconstruction, shared by every report.

Before this, `scripts/dashboard_report.py` and `scripts/win_rate_analysis.py`
each joined fills -> orders -> signals independently and DISAGREED about
which trades exist: 195 "closed" vs 221, a 13% gap, from the same database.
Neither said so. dashboard_report treated any signal whose exit quantity
didn't exactly match its entry as still open; win_rate_analysis included
those, priced on the matched quantity, and counted a partially-closed and
still-running position as a settled win or loss. Running both gave two
different win rates and two different net P&Ls with no reconciliation.

This module is the single source of truth. Crucially it does not silently
exclude anything -- every trade carries an explicit `status`, so each report
states what it is leaving out rather than quietly applying its own rule.

It also reports COVERAGE, because the honest headline about this journal is
that a large share of exits were never recorded at all (orders.signal_id was
NOT NULL until 2026-09-30, so an unattributable flatten could not be written).
A P&L figure drawn from a minority of traded notional should never be
presented as if it were the whole picture.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from warrior_bot.utils.time_utils import EASTERN

# Trades whose entry and exit quantities match to within this many shares
# are treated as flat. Fills are floats and a proportionally-split flatten
# can leave sub-share residue.
_QTY_EPSILON = 1e-6


def et_day_utc_bounds(day: date) -> tuple[str, str]:
    """[start, end) UTC ISO8601 bounds for one ET calendar day.

    Journal timestamps are UTC ISO8601 strings, which sort correctly under
    plain string comparison -- no SQLite date functions needed. Comparing an
    ET date against them directly (as win_rate_analysis.py's --since did)
    is wrong by the UTC offset: a trade at 20:00 ET on the 24th is
    2026-09-25T00:00Z and would be counted on the wrong day.
    """
    start_et = datetime(day.year, day.month, day.day, 0, 0, tzinfo=EASTERN)
    end_et = start_et + timedelta(days=1)
    return start_et.astimezone(timezone.utc).isoformat(), end_et.astimezone(timezone.utc).isoformat()


@dataclass
class Trade:
    """One signal's round trip, as far as the journal can actually attest."""

    signal_id: int
    ts: str
    symbol: str
    strategy: str
    side: str
    planned_entry: float | None
    stop_price: float | None
    target_price: float | None
    sized_qty: int | None
    entry_qty: float
    avg_entry: float | None
    exit_qty: float
    avg_exit: float | None
    closed_qty: float
    commission_total: float
    gross_pnl: float
    net_pnl: float
    status: str
    exit_roles: set[str] = field(default_factory=set)
    first_exec_ts: str | None = None
    last_exec_ts: str | None = None
    duration_minutes: float | None = None
    duration_source: str = "none"
    context_json: str | None = None

    @property
    def is_settled(self) -> bool:
        """Safe to count in a win rate: fully round-tripped, nothing left."""
        return self.status == "closed"


# status values, in plain terms:
#   closed        entry and exit quantities match -- a genuine round trip
#   partial       some of the position was closed, the rest is still open
#   open          filled and still held; no exit recorded (NOTE: may instead
#                 mean the exit happened but was never journaled -- see the
#                 module docstring)
#   oversold      more was sold than was ever bought (the 2026-09-16 NRXS
#                 naked-short shape); never counted as a win or a loss
#   never_filled  accepted and bracketed, but the entry never filled
STATUSES = ("closed", "partial", "open", "oversold", "never_filled")


_QUERY = """
    SELECT s.id AS signal_id, s.ts AS signal_ts, s.symbol, s.strategy, s.side,
           s.entry_price AS planned_entry, s.stop_price, s.target_price, s.context_json,
           rd.sized_qty,
           o.id AS order_id, o.role, o.action,
           f.id AS fill_id, f.ts AS fill_ts, f.exec_ts, f.fill_qty, f.fill_price, f.commission
    FROM signals s
    JOIN risk_decisions rd ON rd.signal_id = s.id AND rd.decision = 'accepted'
    LEFT JOIN orders o ON o.signal_id = s.id
    LEFT JOIN fills f ON f.order_id = o.id
    ORDER BY s.id, o.id, f.id
"""


def _classify(entry_qty: float, exit_qty: float) -> str:
    if entry_qty <= _QTY_EPSILON:
        return "never_filled"
    if exit_qty <= _QTY_EPSILON:
        return "open"
    if exit_qty > entry_qty + _QTY_EPSILON:
        return "oversold"
    if abs(exit_qty - entry_qty) <= _QTY_EPSILON:
        return "closed"
    return "partial"


def reconstruct_trades(conn: sqlite3.Connection, dedup: bool = True) -> list[Trade]:
    """Every accepted signal's round trip, priced from raw fills.

    Deliberately does NOT use fills.realized_pnl: IBKR's paper simulator
    leaves it ~0 on genuine closing fills, and until 2026-09-30 the bot read
    commissionReport synchronously inside the fill event -- before ib_async
    populates it -- so it was structurally 0.0 everywhere. P&L is recomputed
    from volume-weighted average entry/exit prices.

    `dedup` applies the legacy duplicate-stop-fill workaround for rows
    written before fills.exec_id existed. Post-migration rows are deduped
    exactly by the UNIQUE index at write time and need no heuristic.
    """
    conn.row_factory = sqlite3.Row
    rows = conn.execute(_QUERY).fetchall()
    if dedup:
        rows = _dedup_legacy_stop_fills(rows)

    acc: dict[int, dict] = {}
    for r in rows:
        sig = acc.get(r["signal_id"])
        if sig is None:
            sig = acc[r["signal_id"]] = {
                "row": r,
                "entry_qty": 0.0,
                "entry_notional": 0.0,
                "exit_qty": 0.0,
                "exit_notional": 0.0,
                "commission_total": 0.0,
                "exit_roles": set(),
                "exec_times": [],
            }
        if r["fill_id"] is None:
            continue
        if r["commission"]:
            sig["commission_total"] += r["commission"]
        # exec_ts is IBKR's own execution time; ts is when the row was
        # written. Duration built from the latter measures the bot's
        # event-loop latency, which inflates under exactly the conditions
        # that matter (reconnect backlogs, fill storms).
        stamp = r["exec_ts"] or r["fill_ts"]
        if stamp:
            sig["exec_times"].append((stamp, r["exec_ts"] is not None))
        if r["action"] == "BUY":
            sig["entry_qty"] += r["fill_qty"]
            sig["entry_notional"] += r["fill_qty"] * r["fill_price"]
        elif r["action"] == "SELL":
            sig["exit_qty"] += r["fill_qty"]
            sig["exit_notional"] += r["fill_qty"] * r["fill_price"]
            if r["role"]:
                sig["exit_roles"].add(r["role"])

    trades: list[Trade] = []
    for sig in acc.values():
        r = sig["row"]
        entry_qty, exit_qty = sig["entry_qty"], sig["exit_qty"]
        avg_entry = sig["entry_notional"] / entry_qty if entry_qty else None
        avg_exit = sig["exit_notional"] / exit_qty if exit_qty else None
        # Only the genuinely round-tripped quantity is realized. Pricing an
        # oversold remainder at avg_exit would book an open short as profit.
        closed_qty = min(entry_qty, exit_qty)
        gross = (avg_exit - avg_entry) * closed_qty if (avg_entry is not None and avg_exit and closed_qty) else 0.0

        stamps = sorted(sig["exec_times"])
        duration = None
        source = "none"
        if len(stamps) >= 2:
            try:
                first, last = datetime.fromisoformat(stamps[0][0]), datetime.fromisoformat(stamps[-1][0])
                duration = (last - first).total_seconds() / 60.0
                source = "exec_ts" if all(is_exec for _, is_exec in stamps) else "write_ts"
            except (TypeError, ValueError):
                duration = None

        trades.append(
            Trade(
                signal_id=r["signal_id"],
                ts=r["signal_ts"],
                symbol=r["symbol"],
                strategy=r["strategy"],
                side=r["side"],
                planned_entry=r["planned_entry"],
                stop_price=r["stop_price"],
                target_price=r["target_price"],
                sized_qty=r["sized_qty"],
                entry_qty=entry_qty,
                avg_entry=round(avg_entry, 4) if avg_entry is not None else None,
                exit_qty=exit_qty,
                avg_exit=round(avg_exit, 4) if avg_exit is not None else None,
                closed_qty=closed_qty,
                commission_total=round(sig["commission_total"], 2),
                gross_pnl=round(gross, 2),
                net_pnl=round(gross - sig["commission_total"], 2),
                status=_classify(entry_qty, exit_qty),
                exit_roles=sig["exit_roles"],
                first_exec_ts=stamps[0][0] if stamps else None,
                last_exec_ts=stamps[-1][0] if stamps else None,
                duration_minutes=duration,
                duration_source=source,
                context_json=r["context_json"],
            )
        )
    trades.sort(key=lambda t: t.ts)
    return trades


def in_et_day(trades: list[Trade], day: date) -> list[Trade]:
    start, end = et_day_utc_bounds(day)
    return [t for t in trades if start <= t.ts < end]


@dataclass
class Coverage:
    """How much of what the bot actually traded this data can account for."""

    trade_count: int
    by_status: dict[str, int]
    entry_notional: float
    exit_notional: float
    exit_coverage_pct: float | None
    commissions_all_zero: bool
    duration_from_exec_ts_pct: float | None

    def caveats(self) -> list[str]:
        """Plain-language warnings a report should print about itself."""
        out: list[str] = []
        if self.exit_coverage_pct is not None and self.exit_coverage_pct < 95:
            out.append(
                f"Only {self.exit_coverage_pct:.0f}% of bought notional has a journaled exit -- "
                f"{self.by_status.get('open', 0)} trade(s) read as open. Some of those DID close; "
                "their exits were never recorded (fixed 2026-09-30, but historical rows stay missing)."
            )
        if self.commissions_all_zero and self.trade_count:
            out.append(
                "Every commission is 0.00, so all P&L here is GROSS. Commissions were read before "
                "IBKR sent them (fixed 2026-09-30); real costs would move marginal trades."
            )
        if self.duration_from_exec_ts_pct is not None and self.duration_from_exec_ts_pct < 100:
            out.append(
                f"Only {self.duration_from_exec_ts_pct:.0f}% of durations use IBKR execution times; "
                "the rest measure journal write time, i.e. the bot's own latency."
            )
        if self.by_status.get("partial"):
            out.append(
                f"{self.by_status['partial']} trade(s) are partially closed and still running -- "
                "excluded from win rate rather than scored as settled."
            )
        if self.by_status.get("oversold"):
            out.append(
                f"{self.by_status['oversold']} trade(s) sold more than was bought (an unintended short) -- "
                "excluded from win rate."
            )
        return out


def coverage(trades: list[Trade]) -> Coverage:
    by_status = {s: 0 for s in STATUSES}
    for t in trades:
        by_status[t.status] = by_status.get(t.status, 0) + 1

    entry_notional = sum((t.avg_entry or 0) * t.entry_qty for t in trades)
    exit_notional = sum((t.avg_exit or 0) * t.exit_qty for t in trades)
    with_duration = [t for t in trades if t.duration_minutes is not None]
    from_exec = [t for t in with_duration if t.duration_source == "exec_ts"]

    return Coverage(
        trade_count=len(trades),
        by_status=by_status,
        entry_notional=round(entry_notional, 2),
        exit_notional=round(exit_notional, 2),
        exit_coverage_pct=round(exit_notional / entry_notional * 100, 1) if entry_notional else None,
        commissions_all_zero=all(t.commission_total == 0 for t in trades) if trades else False,
        duration_from_exec_ts_pct=(
            round(len(from_exec) / len(with_duration) * 100, 1) if with_duration else None
        ),
    )


# The double-journal bug (two listeners on one IBKR fill event) was fixed
# on 2026-09-14. The dedup heuristic below is applied ONLY to rows written
# before this, because on later data it does real damage.
#
# Measured on the live journal: of 148 adjacent same-order/quantity/price
# stop-fill pairs, 95 are dated AFTER the fix -- they are genuine repeat
# partials, not duplicates. This bot's thin/low-float universe routinely
# fills one order as a burst of identical-size partials at the same price
# milliseconds apart (MTEN: 23 fills in ~6 seconds), which is exactly the
# shape the heuristic mistakes for a duplicate. Left unbounded it deleted
# real exits: APUS bought 363 and sold 363 -- a clean round trip -- and came
# out of dedup showing 163 sold, reclassifying a closed trade as partial.
_DOUBLE_JOURNAL_FIXED_ON = "2026-09-15"


def _dedup_legacy_stop_fills(rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
    """Drops exact back-to-back duplicate fills on the same stop order,
    for pre-2026-09-15 rows only.

    Knowingly incomplete even there: it filters role='stop' only, while the
    same double-listener shape also produced scale_out overfills (7 of 11
    orders, up to 2.95x) that flow straight through. That is precisely why
    exec_id + a UNIQUE index was added -- post-migration rows are deduped
    exactly at write time and this function never touches them.
    """
    out: list[sqlite3.Row] = []
    prev: sqlite3.Row | None = None
    for r in rows:
        if (
            prev is not None
            and r["fill_id"] is not None
            and prev["fill_id"] is not None
            # Only rows old enough to predate the fix are suspect.
            and (r["fill_ts"] or "") < _DOUBLE_JOURNAL_FIXED_ON
            # exec_id present means the UNIQUE index already guaranteed
            # uniqueness; never second-guess it.
            and not _row_has_exec_id(r)
            and r["role"] == "stop"
            and r["order_id"] == prev["order_id"]
            and r["fill_qty"] == prev["fill_qty"]
            and r["fill_price"] == prev["fill_price"]
            and _within_a_second(prev["fill_ts"], r["fill_ts"])
        ):
            prev = r
            continue
        out.append(r)
        prev = r
    return out


def _row_has_exec_id(row: sqlite3.Row) -> bool:
    try:
        return row["exec_ts"] is not None or row["fill_id"] is None
    except (IndexError, KeyError):
        return False


def _within_a_second(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    try:
        return abs((datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds()) <= 1.0
    except (TypeError, ValueError):
        return False
