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

from warrior_bot.analysis.trades import coverage, et_day_utc_bounds, in_et_day, reconstruct_trades
from warrior_bot.config import load_config
from warrior_bot.persistence.db import get_connection
from warrior_bot.utils.time_utils import to_eastern


def build_report(conn, day: date) -> dict:
    """One ET day's report, sourced from the SHARED reconstruction.

    This used to reimplement the join and classification itself, and
    disagreed with win_rate_analysis.py by 13% on which trades existed --
    two different win rates from one database, with no reconciliation.
    Both now call warrior_bot.analysis.trades.

    The JSON keys are unchanged for compatibility; `coverage` and `caveats`
    are added so a reader cannot mistake the numbers for the whole picture.
    """
    day_trades = in_et_day(reconstruct_trades(conn), day)
    cov = coverage(day_trades)

    trades = [
        {
            "signal_id": t.signal_id,
            "ts": t.ts,
            "symbol": t.symbol,
            "strategy": t.strategy,
            "side": t.side,
            "planned_entry": t.planned_entry,
            "stop_price": t.stop_price,
            "target_price": t.target_price,
            "sized_qty": t.sized_qty,
            "entry_qty": int(t.entry_qty),
            "avg_entry": t.avg_entry,
            "exit_qty": int(t.exit_qty),
            "avg_exit": t.avg_exit,
            # Kept for compatibility; `status` below is the precise answer.
            "still_open": not t.is_settled,
            "status": t.status,
            "realized_pnl_est": t.gross_pnl,
            "commission_total": t.commission_total,
            "net_pnl": t.net_pnl,
            "duration_minutes": round(t.duration_minutes, 2) if t.duration_minutes is not None else None,
            "duration_source": t.duration_source,
        }
        for t in day_trades
    ]

    settled = [t for t in day_trades if t.is_settled]
    wins = [t for t in settled if t.net_pnl > 0]
    losses = [t for t in settled if t.net_pnl < 0]
    summary = {
        "trade_count": len(day_trades),
        "closed_count": len(settled),
        "open_count": len(day_trades) - len(settled),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(settled), 4) if settled else None,
        "net_realized_pnl": round(sum(t.net_pnl for t in settled), 2),
        "best_trade_pnl": max((t.net_pnl for t in settled), default=None),
        "worst_trade_pnl": min((t.net_pnl for t in settled), default=None),
        # P&L above is GROSS while commissions read 0.00 -- see caveats.
        "pnl_is_gross_of_commission": cov.commissions_all_zero,
    }

    return {
        "date": day.isoformat(),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        "coverage": {
            "by_status": cov.by_status,
            "entry_notional": cov.entry_notional,
            "exit_notional": cov.exit_notional,
            "exit_coverage_pct": cov.exit_coverage_pct,
            "duration_from_exec_ts_pct": cov.duration_from_exec_ts_pct,
        },
        "caveats": cov.caveats(),
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
    conn = get_connection(db_path)  # applies pending additive migrations

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
