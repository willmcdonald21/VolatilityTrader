"""Summarize win rate, realized PnL, and average R-multiple per strategy
from the trade journal. This is the primary feedback loop for tuning
strategy parameters, since backtesting this style of setup is unreliable.

Built on warrior_bot.analysis.trades, the single shared reconstruction, so
this agrees with win_rate_analysis.py and dashboard_report.py by
construction. It previously did its own SQL join and summed
`fills.realized_pnl` -- a column that is structurally zero, because the bot
read `fill.commissionReport` synchronously before ib_async had populated it
(fixed 2026-09-30, but historical rows stay zero). That made this report
state a 0.0% win rate and $0.00 P&L for gap_and_go and vwap_reversion
indefinitely, while counting all 374 accepted signals -- never-filled and
still-open included -- as settled "trades".
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from warrior_bot.analysis.trades import coverage, reconstruct_trades
from warrior_bot.config import load_config
from warrior_bot.persistence.db import get_connection

# Ross Cameron's own stated rule: don't draw conclusions about a strategy's
# edge from fewer than ~100 trades -- a handful of losses is statistically
# meaningless noise against a strategy with a genuine (but non-100%) win
# rate, and over-reacting to it is a leading cause of abandoning a working
# strategy prematurely.
MIN_TRADES_FOR_CONCLUSIONS = 100


def main() -> None:
    config = load_config()
    db_path = config.resolve_path(config.journal.db_path)
    # get_connection rather than a raw connect: it applies any pending
    # additive migrations, so this never reads a stale schema.
    conn = get_connection(db_path)

    all_trades = reconstruct_trades(conn)
    # Only settled trades can be scored. A signal that never filled is not a
    # 0% win, and a position still running has no realized result yet --
    # counting them as trades is what produced "180 trades, 0.0% win,
    # $0.00" for gap_and_go.
    trades = [t for t in all_trades if t.status == "closed"]

    cov = coverage(all_trades)
    print("=" * 78)
    print("WHAT THIS REPORT CAN AND CANNOT TELL YOU")
    print("=" * 78)
    counts = ", ".join(f"{n} {s}" for s, n in cov.by_status.items() if n)
    print(f"{cov.trade_count} accepted signals: {counts}")
    print(f"Win rate, P&L and R below cover the {len(trades)} SETTLED trades only.")
    for caveat in cov.caveats():
        print(f"  ! {caveat}")
    print("=" * 78)
    print()

    by_strategy: dict[str, dict] = {}
    for t in trades:
        risk_per_share = (
            abs(t.planned_entry - t.stop_price)
            if t.planned_entry is not None and t.stop_price is not None
            else None
        )
        risk_dollars = (risk_per_share * t.entry_qty) if risk_per_share else 0.0
        r_multiple = t.net_pnl / risk_dollars if risk_dollars > 0 else 0.0
        bucket = by_strategy.setdefault(
            t.strategy, {"trades": 0, "wins": 0, "pnl": 0.0, "r_sum": 0.0, "worst_r": 0.0}
        )
        bucket["trades"] += 1
        if t.net_pnl > 0:
            bucket["wins"] += 1
        bucket["pnl"] += t.net_pnl
        bucket["r_sum"] += r_multiple
        bucket["worst_r"] = min(bucket["worst_r"], r_multiple)

    if not by_strategy:
        print("No settled trades in the journal yet.")
    else:
        # Worst-single-trade R is tracked separately from the average R
        # above deliberately: an average can look fine while still masking
        # an occasional outsized loss (10-20x a typical loss) that did
        # real account-level damage the average alone wouldn't reveal.
        print(f"{'Strategy':<16} {'Trades':>7} {'Win%':>7} {'PnL':>10} {'Avg R':>8} {'Worst R':>8}")
        low_sample_strategies = []
        for strategy, b in sorted(by_strategy.items()):
            win_pct = (b["wins"] / b["trades"] * 100) if b["trades"] else 0.0
            avg_r = b["r_sum"] / b["trades"] if b["trades"] else 0.0
            flag = " *" if b["trades"] < MIN_TRADES_FOR_CONCLUSIONS else ""
            print(
                f"{strategy:<16} {b['trades']:>7} {win_pct:>6.1f}% {b['pnl']:>10.2f} "
                f"{avg_r:>8.2f} {b['worst_r']:>8.2f}{flag}"
            )
            if b["trades"] < MIN_TRADES_FOR_CONCLUSIONS:
                low_sample_strategies.append(strategy)
        if low_sample_strategies:
            print(
                f"\n* fewer than {MIN_TRADES_FOR_CONCLUSIONS} trades ({', '.join(low_sample_strategies)}) -- "
                "too small a sample to judge whether the strategy has a real edge yet."
            )

    rejections = conn.execute(
        "SELECT reason, COUNT(*) as n FROM rejections GROUP BY reason ORDER BY n DESC"
    ).fetchall()
    if rejections:
        print("\nRejections by reason:")
        for r in rejections:
            print(f"  {r['reason']}: {r['n']}")


if __name__ == "__main__":
    main()
