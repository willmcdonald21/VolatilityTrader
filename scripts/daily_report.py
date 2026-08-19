"""Summarize win rate, realized PnL, and average R-multiple per strategy
from the trade journal. This is the primary feedback loop for tuning
strategy parameters, since backtesting this style of setup is unreliable."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from warrior_bot.config import load_config

# Ross Cameron's own stated rule: don't draw conclusions about a strategy's
# edge from fewer than ~100 trades -- a handful of losses is statistically
# meaningless noise against a strategy with a genuine (but non-100%) win
# rate, and over-reacting to it is a leading cause of abandoning a working
# strategy prematurely.
MIN_TRADES_FOR_CONCLUSIONS = 100


def main() -> None:
    config = load_config()
    db_path = config.resolve_path(config.journal.db_path)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        """
        SELECT s.id as signal_id, s.symbol, s.strategy, s.entry_price, s.stop_price,
               rd.sized_qty,
               COALESCE(SUM(f.realized_pnl), 0) as realized_pnl
        FROM signals s
        JOIN risk_decisions rd ON rd.signal_id = s.id AND rd.decision = 'accepted'
        LEFT JOIN orders o ON o.signal_id = s.id
        LEFT JOIN fills f ON f.order_id = o.id
        GROUP BY s.id
        ORDER BY s.ts
        """
    ).fetchall()

    by_strategy: dict[str, dict] = {}
    for row in rows:
        risk_dollars = abs(row["entry_price"] - row["stop_price"]) * row["sized_qty"]
        r_multiple = row["realized_pnl"] / risk_dollars if risk_dollars > 0 else 0.0
        bucket = by_strategy.setdefault(
            row["strategy"], {"trades": 0, "wins": 0, "pnl": 0.0, "r_sum": 0.0, "worst_r": 0.0}
        )
        bucket["trades"] += 1
        if row["realized_pnl"] > 0:
            bucket["wins"] += 1
        bucket["pnl"] += row["realized_pnl"]
        bucket["r_sum"] += r_multiple
        bucket["worst_r"] = min(bucket["worst_r"], r_multiple)

    if not by_strategy:
        print("No accepted trades in the journal yet.")
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
