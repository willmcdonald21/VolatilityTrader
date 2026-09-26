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
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.dashboard_report import dedup_stop_fills
from warrior_bot.config import load_config

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


def build_trades(conn: sqlite3.Connection) -> list[dict]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT s.id AS signal_id, s.ts AS signal_ts, s.symbol, s.strategy, s.side,
               s.entry_price AS planned_entry, s.context_json,
               o.id AS order_id, o.role, o.action,
               f.id AS fill_id, f.ts AS fill_ts, f.fill_qty, f.fill_price, f.commission
        FROM signals s
        JOIN risk_decisions rd ON rd.signal_id = s.id AND rd.decision = 'accepted'
        LEFT JOIN orders o ON o.signal_id = s.id
        LEFT JOIN fills f ON f.order_id = o.id
        ORDER BY s.id, o.id, f.ts
        """
    ).fetchall()
    rows = dedup_stop_fills(rows)

    signals: dict[int, dict] = {}
    for r in rows:
        sig = signals.get(r["signal_id"])
        if sig is None:
            sig = signals[r["signal_id"]] = {
                "signal_id": r["signal_id"],
                "symbol": r["symbol"],
                "strategy": r["strategy"],
                "planned_entry": r["planned_entry"],
                "context_json": r["context_json"],
                "entry_qty": 0.0,
                "entry_notional": 0.0,
                "exit_qty": 0.0,
                "exit_notional": 0.0,
                "commission_total": 0.0,
                "exit_roles": set(),
                "first_fill_ts": None,
                "last_fill_ts": None,
            }
        if r["fill_id"] is None:
            continue
        if r["fill_ts"] is not None:
            if sig["first_fill_ts"] is None or r["fill_ts"] < sig["first_fill_ts"]:
                sig["first_fill_ts"] = r["fill_ts"]
            if sig["last_fill_ts"] is None or r["fill_ts"] > sig["last_fill_ts"]:
                sig["last_fill_ts"] = r["fill_ts"]
        if r["commission"]:
            sig["commission_total"] += r["commission"]
        if r["action"] == "BUY":
            sig["entry_qty"] += r["fill_qty"]
            sig["entry_notional"] += r["fill_qty"] * r["fill_price"]
        elif r["action"] == "SELL":
            sig["exit_qty"] += r["fill_qty"]
            sig["exit_notional"] += r["fill_qty"] * r["fill_price"]
            sig["exit_roles"].add(r["role"])

    trades = []
    for sig in signals.values():
        if sig["entry_qty"] == 0 or sig["exit_qty"] == 0:
            continue  # never opened, or opened but never closed -- see dashboard_report.py's still_open handling
        avg_entry = sig["entry_notional"] / sig["entry_qty"]
        avg_exit = sig["exit_notional"] / sig["exit_qty"]
        closed_qty = min(sig["entry_qty"], sig["exit_qty"])  # matched portion only -- see dashboard_report.py
        pnl = round((avg_exit - avg_entry) * closed_qty - sig["commission_total"], 2)
        duration_minutes = None
        if sig["first_fill_ts"] and sig["last_fill_ts"]:
            duration_minutes = (
                datetime.fromisoformat(sig["last_fill_ts"]) - datetime.fromisoformat(sig["first_fill_ts"])
            ).total_seconds() / 60.0
        trades.append(
            {
                "symbol": sig["symbol"],
                "strategy": sig["strategy"],
                "pnl": pnl,
                "exit_roles": sig["exit_roles"],
                "duration_minutes": duration_minutes,
                "extension_pct": _extension_pct(sig["context_json"], avg_entry),
                "open_ts": sig["first_fill_ts"],
            }
        )
    return trades


def _print_bucket_table(title: str, buckets: dict[str, list[dict]]) -> None:
    print(f"\n{title}")
    print(f"{'Bucket':<14} {'N':>5} {'Win%':>7} {'PF':>8} {'Total P&L':>12}")
    for label, trades in buckets.items():
        wins = [t for t in trades if t["pnl"] > 0]
        losses = [t for t in trades if t["pnl"] <= 0]
        gross_win = sum(t["pnl"] for t in wins)
        gross_loss = -sum(t["pnl"] for t in losses)
        pf = (gross_win / gross_loss) if gross_loss > 0 else float("inf") if gross_win > 0 else 0.0
        win_pct = len(wins) / len(trades) * 100 if trades else 0.0
        flag = " *" if len(trades) < MIN_TRADES_FOR_CONCLUSIONS else ""
        pf_str = "inf" if pf == float("inf") else f"{pf:.2f}"
        print(f"{label:<14} {len(trades):>5} {win_pct:>6.1f}% {pf_str:>8} {sum(t['pnl'] for t in trades):>12.2f}{flag}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", help="Only include trades opened on/after this ET date (YYYY-MM-DD)")
    parser.add_argument("--until", help="Only include trades opened before this ET date (YYYY-MM-DD)")
    args = parser.parse_args()

    config = load_config()
    db_path = config.resolve_path(config.journal.db_path)
    conn = sqlite3.connect(str(db_path))

    trades = build_trades(conn)
    if args.since:
        trades = [t for t in trades if t["open_ts"] and t["open_ts"] >= args.since]
    if args.until:
        trades = [t for t in trades if t["open_ts"] and t["open_ts"] < args.until]

    if not trades:
        print("No closed trades found in the journal for this range.")
        return

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = -sum(t["pnl"] for t in losses)
    pf = (gross_win / gross_loss) if gross_loss > 0 else float("inf") if gross_win > 0 else 0.0
    print(f"Overall: {len(trades)} closed trades, {len(wins)/len(trades)*100:.1f}% win rate, "
          f"profit factor {pf if pf == float('inf') else round(pf, 2)}, net P&L {sum(t['pnl'] for t in trades):.2f}")
    if len(trades) < MIN_TRADES_FOR_CONCLUSIONS:
        print(f"* fewer than {MIN_TRADES_FOR_CONCLUSIONS} trades total -- too small a sample to draw conclusions from yet.")

    by_strategy: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        by_strategy[t["strategy"]].append(t)
    _print_bucket_table("By strategy:", dict(sorted(by_strategy.items())))

    by_exit_role: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        if "stop" in t["exit_roles"]:
            key = "stop"
        elif t["exit_roles"] & {"target", "scale_out"}:
            key = "target/scale_out"
        elif t["exit_roles"]:
            key = ",".join(sorted(t["exit_roles"]))
        else:
            key = "unknown"
        by_exit_role[key].append(t)
    _print_bucket_table("By exit role:", dict(sorted(by_exit_role.items(), key=lambda kv: -len(kv[1]))))

    by_duration: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        if t["duration_minutes"] is None:
            continue
        label = _bucket(t["duration_minutes"], DURATION_BUCKETS)
        if label:
            by_duration[label].append(t)
    ordered_duration = {label: by_duration[label] for label, _, _ in DURATION_BUCKETS if label in by_duration}
    _print_bucket_table("By trade duration:", ordered_duration)

    for strategy_name in ("gap_and_go", "vwap_reversion"):
        by_extension: dict[str, list[dict]] = defaultdict(list)
        for t in trades:
            if t["strategy"] != strategy_name or t["extension_pct"] is None:
                continue
            label = _bucket(t["extension_pct"], EXTENSION_BUCKETS)
            if label:
                by_extension[label].append(t)
        if by_extension:
            ordered_extension = {label: by_extension[label] for label, _, _ in EXTENSION_BUCKETS if label in by_extension}
            _print_bucket_table(f"By entry-extension bucket ({strategy_name}):", ordered_extension)


if __name__ == "__main__":
    main()
