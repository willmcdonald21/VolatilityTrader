from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    strategy TEXT NOT NULL,
    side TEXT NOT NULL,
    entry_price REAL NOT NULL,
    stop_price REAL NOT NULL,
    target_price REAL NOT NULL,
    context_json TEXT
);

CREATE TABLE IF NOT EXISTS risk_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER NOT NULL REFERENCES signals(id),
    ts TEXT NOT NULL,
    decision TEXT NOT NULL,
    reason TEXT,
    sized_qty INTEGER NOT NULL,
    equity_snapshot REAL,
    daily_pnl_snapshot REAL,
    open_positions_count INTEGER
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER NOT NULL REFERENCES signals(id),
    ib_order_id INTEGER,
    role TEXT NOT NULL,
    action TEXT NOT NULL,
    qty REAL NOT NULL,
    order_type TEXT NOT NULL,
    limit_price REAL,
    stop_price REAL,
    oca_group TEXT,
    status TEXT,
    ts_submitted TEXT NOT NULL,
    ts_last_update TEXT
);

CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER REFERENCES orders(id),
    ib_order_id INTEGER,
    ts TEXT NOT NULL,
    fill_qty REAL NOT NULL,
    fill_price REAL NOT NULL,
    commission REAL,
    realized_pnl REAL
);

CREATE TABLE IF NOT EXISTS rejections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    strategy TEXT NOT NULL,
    reason TEXT NOT NULL,
    detail_json TEXT
);

CREATE TABLE IF NOT EXISTS account_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    net_liquidation REAL,
    buying_power REAL,
    daily_realized_pnl REAL,
    open_positions_count INTEGER
);

CREATE TABLE IF NOT EXISTS kill_switch_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    triggered_by TEXT NOT NULL,
    action_taken TEXT NOT NULL
);

-- One row per ET trading date: the day's start-of-day equity baseline and
-- whether the daily loss limit has already halted new entries for it.
-- Restored on every process start (see WarriorBot.start) so a same-day
-- restart -- crash, supervisor, or a manual one mid-session -- can't reset
-- an active halt back to False, or the baseline to whatever equity happens
-- to be at restart time. Confirmed live, 2026-09-23: three restarts in
-- quick succession (07:57, 10:52, 11:01 ET) each undid the prior breach's
-- halt, re-exposing the account to new entries three separate times on a
-- day already over its loss limit.
CREATE TABLE IF NOT EXISTS daily_risk_state (
    trading_date TEXT PRIMARY KEY,
    start_of_day_equity REAL NOT NULL,
    loss_limit_halted INTEGER NOT NULL DEFAULT 0,
    ts_updated TEXT NOT NULL
);
"""


def get_connection(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn
