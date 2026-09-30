"""Win-rate/profit-factor breakdown across the WHOLE trade journal (not one
day) -- by strategy, by entry-extension bucket, by trade duration, and by
which exit role actually closed the trade. Written 2026-09-25 to formalize
the ad hoc analysis behind that day's win-rate review (see
docs/strategy_decisions.md and the config.yaml/config.py comments it
produced) into something repeatable, instead of rebuilding it from scratch
each time the bot's entry/exit tuning gets revisited.

Reuses dashboard_report.py's per-signal reconstruction approach (join
fills -> orders -> signals, average-cost from raw fill prices, dedup the
2026-09-14 duplicate-stop-fill bug) rather than trusting fills.realized_pnl,
which IBKR's paper simulator reports as ~0 on genuine closing fills (see
warrior_bot/risk/account_state.py's daily_realized_pnl docstring) --
daily_report.py's SUM(f.realized_pnl) approach is affected by this and
should not be used for win-rate conclusions.

Every bucket below is flagged once its sample size drops under
MIN_TRADES_FOR_CONCLUSIONS (daily_report.py's own constant, Ross Cameron's
stated minimum-sample-size rule) -- direction of a signal can still be worth
noting below that bar, but shouldn't be treated as a settled conclusion.

Usage:
    python scripts/win_rate_analysis.py [--since YYYY-MM-DD] [--until YYYY-MM-DD]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from warrior_bot.analysis.trades import coverage, et_day_utc_bounds, reconstruct_trades
from warrior_bot.config import load_config
from warrior_bot.persistence.db import get_connection

MIN_TRADES_FOR_CONCLUSIONS = 100

DURATION_BUCKETS = [
    ("<2min", 0, 2),
    ("2-10min", 2, 10),
    ("10-30min", 10, 30),
    ("30-120min", 30, 120),
    ("120min+", 120, float("inf")),
]

EXTENSION_BUCKETS = [
    ("0-1%", 0, 1),
    ("1-2%", 1, 2),
    ("2-3%", 2, 3),
    ("3-5%", 3, 5),
    ("5-10%", 5, 10),
    (">=10%", 10, float("inf")),
]


def _bucket(value: float, buckets: list[tuple[str, float, float]]) -> str | None:
    for label, lo, hi in buckets:
        if lo <= value < hi:
            return label
    return None


def _extension_pct(context_json: str | None, entry_price: float) -> float | None:
    """Reconstructs how far past its own trigger level a signal's entry
    priced, from the same fields is_entry_too_extended's callers already
    put in context_json -- breakout_high (gap_and_go) or vwap (vwap_reversion
    bounce). red_to_green's trigger (prior_close) isn't in context_json, so
    those signals come back None (excluded, not miscounted as 0%)."""
    if not context_json:
        return None
    try:
        ctx = json.loads(context_json)
    except (TypeError, ValueError):
        return None
    trigger_level = ctx.get("breakout_high") or ctx.get("vwap")
    if not trigger_level or trigger_level <= 0:
        return None
    return (entry_price - trigger_level) / trigger_level * 100.0


def _print_bucket_table(title: str, buckets: dict[str, list[dict]]) -> None:
    print(f"\n{title}")
    print(f"{'Bucket':<14} {'N':>5} {'Win%':>7} {'PF':>8} {'Total P&L':>12}")
    for label, trades in buckets.items():
        wins = [t for t in trades if t.net_pnl > 0]
        losses = [t for t in trades if t.net_pnl <= 0]
        gross_win = sum(t.net_pnl for t in wins)
        gross_loss = -sum(t.net_pnl for t in losses)
        pf = (gross_win / gross_loss) if gross_loss > 0 else float("inf") if gross_win > 0 else 0.0
        win_pct = len(wins) / len(trades) * 100 if trades else 0.0
        flag = " *" if len(trades) < MIN_TRADES_FOR_CONCLUSIONS else ""
        pf_str = "inf" if pf == float("inf") else f"{pf:.2f}"
        print(f"{label:<14} {len(trades):>5} {win_pct:>6.1f}% {pf_str:>8} {sum(t.net_pnl for t in trades):>12.2f}{flag}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", help="Only include trades opened on/after this ET date (YYYY-MM-DD)")
    parser.add_argument("--until", help="Only include trades opened before this ET date (YYYY-MM-DD)")
    args = parser.parse_args()

    config = load_config()
    db_path = config.resolve_path(config.journal.db_path)
    # get_connection rather than a raw connect: it applies any pending
    # additive migrations, so a report can never read a stale schema.
    conn = get_connection(db_path)

    all_trades = reconstruct_trades(conn)

    # ET-date filtering via UTC bounds. Comparing an ET date string directly
    # against a UTC timestamp (as this did before) is wrong by the offset:
    # a trade at 20:00 ET on the 24th is 2026-09-25T00:00Z.
    trades = all_trades
    if args.since:
        start, _ = et_day_utc_bounds(date.fromisoformat(args.since))
        trades = [t for t in trades if t.ts >= start]
    if args.until:
        start, _ = et_day_utc_bounds(date.fromisoformat(args.until))
        trades = [t for t in trades if t.ts < start]

    if not trades:
        print("No trades found in the journal for this range.")
        return

    cov = coverage(trades)
    settled = [t for t in trades if t.is_settled]

    print("=" * 78)
    print("WHAT THIS REPORT CAN AND CANNOT TELL YOU")
    print("=" * 78)
    print(f"{cov.trade_count} accepted signals: " + ", ".join(
        f"{n} {status}" for status, n in cov.by_status.items() if n
    ))
    print(f"Win rate and P&L below cover the {len(settled)} SETTLED (fully round-tripped) trades only.")
    for caveat in cov.caveats():
        print(f"  ! {caveat}")
    print("=" * 78)
    print()

    if not settled:
        print("No settled trades in this range -- nothing to compute a win rate from.")
        return

    wins = [t for t in settled if t.net_pnl > 0]
    losses = [t for t in settled if t.net_pnl <= 0]
    gross_win = sum(t.net_pnl for t in wins)
    gross_loss = -sum(t.net_pnl for t in losses)
    pf = (gross_win / gross_loss) if gross_loss > 0 else float("inf") if gross_win > 0 else 0.0
    print(
        f"Overall: {len(settled)} settled trades, {len(wins) / len(settled) * 100:.1f}% win rate, "
        f"profit factor {pf if pf == float('inf') else round(pf, 2)}, net P&L {sum(t.net_pnl for t in settled):.2f}"
    )
    if len(settled) < MIN_TRADES_FOR_CONCLUSIONS:
        print(f"* fewer than {MIN_TRADES_FOR_CONCLUSIONS} settled trades -- too small a sample to conclude from.")

    by_strategy: dict[str, list] = defaultdict(list)
    for t in settled:
        by_strategy[t.strategy].append(t)
    _print_bucket_table("By strategy:", dict(sorted(by_strategy.items())))

    by_exit_role: dict[str, list] = defaultdict(list)
    for t in settled:
        if "stop" in t.exit_roles:
            key = "stop"
        elif t.exit_roles & {"target", "scale_out"}:
            key = "target/scale_out"
        elif t.exit_roles:
            key = ",".join(sorted(t.exit_roles))
        else:
            key = "unknown"
        by_exit_role[key].append(t)
    _print_bucket_table("By exit role:", dict(sorted(by_exit_role.items(), key=lambda kv: -len(kv[1]))))

    by_duration: dict[str, list] = defaultdict(list)
    for t in settled:
        if t.duration_minutes is None:
            continue
        label = _bucket(t.duration_minutes, DURATION_BUCKETS)
        if label:
            by_duration[label].append(t)
    ordered = {label: by_duration[label] for label, _, _ in DURATION_BUCKETS if label in by_duration}
    _print_bucket_table("By trade duration:", ordered)

    for strategy_name in ("gap_and_go", "vwap_reversion"):
        by_extension: dict[str, list] = defaultdict(list)
        for t in settled:
            if t.strategy != strategy_name:
                continue
            ext = _extension_pct(t.context_json, t.avg_entry or 0.0)
            if ext is None:
                continue
            label = _bucket(ext, EXTENSION_BUCKETS)
            if label:
                by_extension[label].append(t)
        if by_extension:
            ordered_ext = {label: by_extension[label] for label, _, _ in EXTENSION_BUCKETS if label in by_extension}
            _print_bucket_table(f"By entry-extension bucket ({strategy_name}):", ordered_ext)


if __name__ == "__main__":
    main()
