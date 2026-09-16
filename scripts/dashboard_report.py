"""Generate one trading day's JSON report for the persistent dashboard
artifact: every accepted signal that day, with actual (not planned) entry/
exit prices, realized P&L, and a day-level summary.

Deliberately does NOT trust `fills.realized_pnl` -- confirmed against this
bot's real paper-trading data that it comes back ~0 for genuine exits even
though the write-time UNSET_DOUBLE-sentinel filtering (see
warrior_bot/risk/account_state.py) is technically correct; IBKR's paper
simulator just doesn't populate it reliably per partial fill. P&L here is
computed from volume-weighted average entry/exit prices across raw fills
instead, the same method validated by hand against a full day's fills on
2026-09-14.

Usage:
    python scripts/dashboard_report.py [--date YYYY-MM-DD] [--out path.json]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from warrior_bot.config import load_config
from warrior_bot.utils.time_utils import EASTERN, to_eastern


def et_day_utc_bounds(day: date) -> tuple[str, str]:
    """[start, end) UTC ISO8601 bounds for one ET calendar day. Journal
    timestamps are UTC ISO8601 strings (see warrior_bot/persistence/journal.py
    _now()), which sort correctly under plain string comparison -- no SQLite
    date functions needed."""
    start_et = datetime(day.year, day.month, day.day, 0, 0, tzinfo=EASTERN)
    end_et = start_et + timedelta(days=1)
    return start_et.astimezone(timezone.utc).isoformat(), end_et.astimezone(timezone.utc).isoformat()


def dedup_stop_fills(rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
    """Drops exact back-to-back duplicate fills on the same stop order.

    A real bug (fixed 2026-09-14, warrior_bot/execution/position_manager.py
    track()/_wire_stop_fill()/_on_stop_fill()) double-journaled every fill on
    an original, non-replaced stop-loss order -- two listeners on the same
    IBKR fill event. Data recorded before that fix can contain an exact
    (order_id, fill_qty, fill_price) match within ~1 second of a prior row
    for role='stop'. This is a no-op on post-fix data: two genuinely
    separate partial fills at the same price, seconds apart, are never
    dropped, since the timestamp gate is 1 second, and a real duplicate
    pair (from the bug) always lands within milliseconds of each other.
    """
    out: list[sqlite3.Row] = []
    prev: sqlite3.Row | None = None
    for r in rows:
        if (
            prev is not None
            and r["role"] == "stop"
            and prev["order_id"] == r["order_id"]
            and r["fill_qty"] == prev["fill_qty"]
            and r["fill_price"] == prev["fill_price"]
            and r["fill_ts"] is not None
            and prev["fill_ts"] is not None
            and (datetime.fromisoformat(r["fill_ts"]) - datetime.fromisoformat(prev["fill_ts"])).total_seconds() < 1.0
        ):
            prev = r
            continue
        out.append(r)
        prev = r
    return out


def build_report(conn: sqlite3.Connection, day: date) -> dict:
    # Self-contained regardless of how the caller configured `conn` -- every
    # column access below is by name.
    conn.row_factory = sqlite3.Row
    start_utc, end_utc = et_day_utc_bounds(day)
    rows = conn.execute(
        """
        SELECT s.id AS signal_id, s.ts AS signal_ts, s.symbol, s.strategy, s.side,
               s.entry_price AS planned_entry, s.stop_price, s.target_price,
               rd.sized_qty,
               o.id AS order_id, o.role, o.action,
               f.id AS fill_id, f.ts AS fill_ts, f.fill_qty, f.fill_price, f.commission
        FROM signals s
        JOIN risk_decisions rd ON rd.signal_id = s.id AND rd.decision = 'accepted'
        LEFT JOIN orders o ON o.signal_id = s.id
        LEFT JOIN fills f ON f.order_id = o.id
        WHERE s.ts >= ? AND s.ts < ?
        ORDER BY s.id, o.id, f.ts
        """,
        (start_utc, end_utc),
    ).fetchall()

    rows = dedup_stop_fills(rows)

    signals: dict[int, dict] = {}
    for r in rows:
        sig = signals.get(r["signal_id"])
        if sig is None:
            # Long-only bot (Signal.side is always "BUY" per
            # warrior_bot/signals/signal.py) -- the P&L math below assumes
            # BUY-to-open/SELL-to-close. Fail loudly rather than silently
            # mis-computing P&L if a short-side signal is ever introduced.
            assert r["side"] == "BUY", f"signal {r['signal_id']} ({r['symbol']}) has side={r['side']!r}, not BUY"
            sig = signals[r["signal_id"]] = {
                "signal_id": r["signal_id"],
                "ts": r["signal_ts"],
                "symbol": r["symbol"],
                "strategy": r["strategy"],
                "side": r["side"],
                "planned_entry": r["planned_entry"],
                "stop_price": r["stop_price"],
                "target_price": r["target_price"],
                "sized_qty": r["sized_qty"],
                "entry_qty": 0.0,
                "entry_notional": 0.0,
                "exit_qty": 0.0,
                "exit_notional": 0.0,
                "commission_total": 0.0,
            }
        if r["fill_id"] is None:
            continue
        if r["commission"]:
            sig["commission_total"] += r["commission"]
        if r["action"] == "BUY":
            sig["entry_qty"] += r["fill_qty"]
            sig["entry_notional"] += r["fill_qty"] * r["fill_price"]
        elif r["action"] == "SELL":
            sig["exit_qty"] += r["fill_qty"]
            sig["exit_notional"] += r["fill_qty"] * r["fill_price"]

    trades = []
    for sig in signals.values():
        avg_entry = sig["entry_notional"] / sig["entry_qty"] if sig["entry_qty"] else None
        avg_exit = sig["exit_notional"] / sig["exit_qty"] if sig["exit_qty"] else None
        # A residual position remains whenever bought != sold, in EITHER
        # direction -- not just under-filled (still holding some of the
        # original long). Confirmed live (GVH, 2026-09-14): a stop-sizing
        # bug let exit_qty exceed entry_qty (sold more than was ever
        # bought, a naked short), and exit_qty < entry_qty alone reads
        # that as a normal fully-closed winning trade, silently absorbing
        # the open short into a plain P&L number instead of flagging it.
        # A signal with zero entry fills was also never actually opened
        # (order rejected/never filled) -- "open" (incomplete), not a
        # closed flat trade -- so it can't fall out of this check just
        # because both sides happen to be 0.
        still_open = sig["entry_qty"] == 0 or sig["exit_qty"] != sig["entry_qty"]
        # Only the genuinely round-tripped quantity is "realized" -- for
        # the GVH case above, that's min(3943, 5384) = 3943, not the full
        # exit_qty. Pricing the extra 1441 sold-but-never-bought shares
        # at avg_exit would count an open, unclosed short as if it were
        # locked-in profit.
        closed_qty = min(sig["entry_qty"], sig["exit_qty"])
        realized_pnl_est = (avg_exit - avg_entry) * closed_qty if (avg_entry is not None and closed_qty) else 0.0
        net_pnl = round(realized_pnl_est - sig["commission_total"], 2)
        trades.append(
            {
                "signal_id": sig["signal_id"],
                "ts": sig["ts"],
                "symbol": sig["symbol"],
                "strategy": sig["strategy"],
                "side": sig["side"],
                "planned_entry": sig["planned_entry"],
                "stop_price": sig["stop_price"],
                "target_price": sig["target_price"],
                "sized_qty": sig["sized_qty"],
                "entry_qty": int(sig["entry_qty"]),
                "avg_entry": round(avg_entry, 4) if avg_entry is not None else None,
                "exit_qty": int(sig["exit_qty"]),
                "avg_exit": round(avg_exit, 4) if avg_exit is not None else None,
                "still_open": still_open,
                "realized_pnl_est": round(realized_pnl_est, 2),
                "commission_total": round(sig["commission_total"], 2),
                "net_pnl": net_pnl,
            }
        )
    trades.sort(key=lambda t: t["ts"])

    closed = [t for t in trades if not t["still_open"]]
    wins = [t for t in closed if t["net_pnl"] > 0]
    losses = [t for t in closed if t["net_pnl"] < 0]
    summary = {
        "trade_count": len(trades),
        "closed_count": len(closed),
        "open_count": len(trades) - len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(closed), 4) if closed else None,
        "net_realized_pnl": round(sum(t["net_pnl"] for t in closed), 2),
        "best_trade_pnl": max((t["net_pnl"] for t in closed), default=None),
        "worst_trade_pnl": min((t["net_pnl"] for t in closed), default=None),
    }

    return {
        "date": day.isoformat(),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        "trades": trades,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", help="ET date as YYYY-MM-DD (default: today in ET)")
    parser.add_argument("--out", help="Write JSON to this path instead of stdout")
    args = parser.parse_args()

    day = date.fromisoformat(args.date) if args.date else to_eastern(datetime.now(timezone.utc)).date()

    config = load_config()
    db_path = config.resolve_path(config.journal.db_path)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    report = build_report(conn, day)
    output = json.dumps(report, indent=2)

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(output)
        print(f"Wrote {out_path} ({report['summary']['trade_count']} trades, net ${report['summary']['net_realized_pnl']})")
    else:
        print(output)


if __name__ == "__main__":
    main()
