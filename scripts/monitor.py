"""Live, plain-English view of what the bot is doing.

Polls the trade journal DB (signals, risk decisions, orders, fills,
kill-switch events) for anything trading-related -- structured data, not
raw log text -- and tails the log file only for connection/watchlist
status. Read-only: never touches IBKR, safe to run alongside the bot.
"""

from __future__ import annotations

import re
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from warrior_bot.config import load_config

ONBOARD_RE = re.compile(r"Onboarded (\S+): prior_close=(\S+) avg_daily_volume=(\S+) bars=(\d+)")
CONNECTED_RE = re.compile(r"Connected\. Server version=(\d+)")
STARTED_RE = re.compile(r"WarriorBot started: mode=(\S+) strategies=(.+)")
FLATTEN_RE = re.compile(r"Flattening all positions: reason=(\S+)")
REVERSAL_RE = re.compile(r"Reversal exit for (\S+): (\S+) \(qty=(\d+)\)")


def _fmt_ts(iso_ts: str) -> str:
    return iso_ts[11:19] if len(iso_ts) >= 19 else iso_ts


def watch_log(log_path: Path, pos: int, watchlist: set[str]) -> int:
    if not log_path.exists():
        return pos
    with open(log_path, "r", encoding="utf-8") as f:
        f.seek(pos)
        for line in f:
            line = line.rstrip("\n")
            if m := STARTED_RE.search(line):
                print(f"[STARTED]  mode={m.group(1)}  strategies={m.group(2)}")
            elif m := CONNECTED_RE.search(line):
                print(f"[CONNECTED] IB server version {m.group(1)}")
            elif m := ONBOARD_RE.search(line):
                sym, prior_close, avg_vol, bars = m.groups()
                if sym not in watchlist:
                    watchlist.add(sym)
                    try:
                        vol_fmt = f"{float(avg_vol):,.0f}"
                    except ValueError:
                        vol_fmt = avg_vol
                    print(f"[WATCHING] {sym:<6} prior close ${prior_close}  avg daily vol ~{vol_fmt}  ({len(watchlist)} symbols total)")
            elif m := REVERSAL_RE.search(line):
                sym, reasons, qty = m.groups()
                print(f"[EXIT]     {sym}: reversal exit ({reasons.replace(',', ', ')}) qty={qty}")
            elif m := FLATTEN_RE.search(line):
                print(f"[FLATTEN]  {m.group(1)} -- all positions closed, all orders cancelled")
        pos = f.tell()
    return pos


def watch_journal(conn: sqlite3.Connection, last_ids: dict[str, int]) -> None:
    cur = conn.cursor()

    cur.execute("SELECT id, ts, symbol, strategy, side, entry_price, stop_price, target_price FROM signals WHERE id > ? ORDER BY id", (last_ids["signals"],))
    signals = {row[0]: row for row in cur.fetchall()}
    for row in signals.values():
        sid, ts, symbol, strategy, side, entry, stop, target = row
        risk = abs(entry - stop)
        reward = abs(target - entry)
        rr = reward / risk if risk else 0
        print(f"[SIGNAL]   {_fmt_ts(ts)} {symbol} {strategy} {side} entry=${entry:.2f} stop=${stop:.2f} target=${target:.2f} (R:R 1:{rr:.1f})")
        last_ids["signals"] = max(last_ids["signals"], sid)

    cur.execute("SELECT id, ts, symbol, strategy, reason FROM rejections WHERE id > ? ORDER BY id", (last_ids["rejections"],))
    for rid, ts, symbol, strategy, reason in cur.fetchall():
        print(f"[REJECTED] {_fmt_ts(ts)} {symbol} {strategy} -- {reason}")
        last_ids["rejections"] = max(last_ids["rejections"], rid)

    cur.execute(
        "SELECT o.id, o.ts_submitted, s.symbol, o.role, o.action, o.qty, o.order_type, o.limit_price, o.stop_price "
        "FROM orders o JOIN signals s ON s.id = o.signal_id WHERE o.id > ? ORDER BY o.id",
        (last_ids["orders"],),
    )
    for oid, ts, symbol, role, action, qty, order_type, limit_price, stop_price in cur.fetchall():
        price = limit_price if limit_price is not None else stop_price
        print(f"[ORDER]    {_fmt_ts(ts)} {symbol} {role:<10} {action} {qty:g} @ {order_type} ${price:.2f}" if price is not None else
              f"[ORDER]    {_fmt_ts(ts)} {symbol} {role:<10} {action} {qty:g} {order_type}")
        last_ids["orders"] = max(last_ids["orders"], oid)

    cur.execute(
        "SELECT f.id, f.ts, s.symbol, f.fill_qty, f.fill_price, f.realized_pnl "
        "FROM fills f JOIN orders o ON o.id = f.order_id JOIN signals s ON s.id = o.signal_id WHERE f.id > ? ORDER BY f.id",
        (last_ids["fills"],),
    )
    for fid, ts, symbol, fill_qty, fill_price, realized_pnl in cur.fetchall():
        pnl_str = f"  realized P&L=${realized_pnl:.2f}" if realized_pnl is not None else ""
        print(f"[FILL]     {_fmt_ts(ts)} {symbol} {fill_qty:g} shares @ ${fill_price:.2f}{pnl_str}")
        last_ids["fills"] = max(last_ids["fills"], fid)

    cur.execute("SELECT id, ts, triggered_by, action_taken FROM kill_switch_events WHERE id > ? ORDER BY id", (last_ids["kill_switch_events"],))
    for kid, ts, triggered_by, action_taken in cur.fetchall():
        print(f"[HALT]     {_fmt_ts(ts)} triggered_by={triggered_by} action={action_taken}")
        last_ids["kill_switch_events"] = max(last_ids["kill_switch_events"], kid)


def main() -> None:
    config = load_config()
    log_path = config.resolve_path(config.logging.file)
    db_path = config.resolve_path(config.journal.db_path)

    print(f"Monitoring {log_path} and {db_path} -- Ctrl+C to stop.\n")

    log_pos = 0
    watchlist: set[str] = set()
    last_ids = {"signals": 0, "rejections": 0, "orders": 0, "fills": 0, "kill_switch_events": 0}
    last_heartbeat = time.monotonic()

    while True:
        log_pos = watch_log(log_path, log_pos, watchlist)
        if db_path.exists():
            conn = sqlite3.connect(str(db_path))
            try:
                watch_journal(conn, last_ids)
            finally:
                conn.close()

        now = time.monotonic()
        if now - last_heartbeat >= 30:
            print(f"[ALIVE]    watching {len(watchlist)} symbols, {last_ids['signals']} signals so far")
            last_heartbeat = now

        time.sleep(2)


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)
    try:
        main()
    except KeyboardInterrupt:
        pass
