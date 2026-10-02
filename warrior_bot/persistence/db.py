from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger("warrior_bot.persistence.db")

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
    -- NULLABLE by design: an exit the bot cannot attribute to an originating
    -- signal (an emergency/EOD flatten for a symbol with no tracked lot) must
    -- still be recordable. While this was NOT NULL those exits could not be
    -- written at all, which is the main reason only ~45% of traded notional
    -- had a journaled exit. `symbol` carries the attribution in that case.
    signal_id INTEGER REFERENCES signals(id),
    symbol TEXT,
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
    ts TEXT NOT NULL,              -- when THIS ROW WAS WRITTEN, not when it executed
    -- IBKR's own globally-unique execution id. The dedup key: without it a
    -- re-delivered fill and a genuine repeat partial at the same size and
    -- price are indistinguishable by construction, which is why
    -- dashboard_report.dedup_stop_fills exists -- and that heuristic only
    -- covers role='stop', missing scale_out overfills of up to 2.95x.
    exec_id TEXT,
    -- IBKR's execution timestamp. `ts` above is event-loop write time, so
    -- duration built from it measures the bot's own latency, inflating under
    -- exactly the conditions that matter (reconnect backlogs, event storms).
    exec_ts TEXT,
    fill_qty REAL NOT NULL,
    fill_price REAL NOT NULL,
    -- Both arrive LATER than the fill itself, via ib.commissionReportEvent,
    -- and are filled in by Journal.update_fill_commission. Reading
    -- fill.commissionReport synchronously inside the fill event (as this bot
    -- did until 2026-09-30) always yields 0.0, live or paper.
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

-- One row per minute while the bot is up, so "was it actually working at
-- 10:15?" becomes a query instead of log archaeology. The watchdogs cover
-- CONNECTION health; nothing covered PRODUCTIVITY, and a bot that is
-- connected, subscribed and silently producing nothing looks exactly like
-- a quiet market. On 2026-09-28 it was disconnected for 5h20m and then ran
-- ~6 hours on a dead scanner, and the only reason anyone noticed was a
-- direct question.
CREATE TABLE IF NOT EXISTS bot_heartbeat (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    connected INTEGER NOT NULL,
    symbols_subscribed INTEGER NOT NULL,
    bars_received_last_min INTEGER NOT NULL,
    signals_today INTEGER NOT NULL,
    open_positions INTEGER NOT NULL,
    breadth INTEGER,
    scanner_refusals INTEGER NOT NULL DEFAULT 0,
    seconds_since_scan REAL
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

-- One row per (ET trading date, symbol): how many lots in that symbol
-- closed net negative that day. This is what RiskManager's
-- symbol_loss_cap gate reads, via PositionManager.losing_lots_today.
-- Exactly the same restart hazard daily_risk_state above exists for:
-- the count lived only in an in-memory Counter, so every supervisor
-- crash-restart zeroed it and re-opened every symbol that had already
-- taken money off us that day. 2026-10-01 alone restarted four times
-- (00:34, 04:19, 09:44, 09:46) off a scanner-timeout loop, and the
-- gate had shipped that same afternoon -- it would have held in tests
-- and done nothing live. last_exit_role/last_realized_pnl are
-- diagnostic only: the gate counts any net-negative finished lot, and
-- whether a red EOD flatten should burn a symbol the way a stop-out
-- does is a question to answer from this data later, not a behavior
-- change to smuggle in alongside the persistence fix.
CREATE TABLE IF NOT EXISTS symbol_loss_state (
    trading_date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    losing_lots INTEGER NOT NULL,
    last_exit_role TEXT,
    last_realized_pnl REAL,
    ts_updated TEXT NOT NULL,
    PRIMARY KEY (trading_date, symbol)
);
"""


# -- migrations -------------------------------------------------------------
#
# SCHEMA above is CREATE TABLE IF NOT EXISTS, which means a brand-new TABLE
# appears on an existing database (that is why bot_heartbeat showed up on
# 2026-09-29) but a new COLUMN never does -- the IF NOT EXISTS makes SQLite
# skip the whole statement, columns and all. Before this there was no
# migration path at all, so any column added to SCHEMA was silently absent
# from the live six-week database.
#
# PRAGMA user_version is used rather than a table: stdlib, atomic, no extra
# schema to bootstrap. Each migration is (version, description, callable);
# they run in order inside one transaction each, and are idempotent because
# the version only advances on success.


# Indices deliberately live here rather than in SCHEMA: on an existing
# database SCHEMA is executed BEFORE migrations, so an index over a column a
# migration has yet to add (fills.exec_id) would fail outright. _ensure_indices
# runs last, once every column is guaranteed present. The journal had exactly
# one index before this -- every report joins signals->risk_decisions->orders
# ->fills, and find_order_by_ib_order_id full-scanned `orders` on every
# reconnect resync, in the hot path.
INDICES = (
    ("ux_fills_exec_id", "CREATE UNIQUE INDEX IF NOT EXISTS ux_fills_exec_id ON fills(exec_id)"),
    ("ix_fills_order_id", "CREATE INDEX IF NOT EXISTS ix_fills_order_id ON fills(order_id)"),
    ("ix_fills_exec_ts", "CREATE INDEX IF NOT EXISTS ix_fills_exec_ts ON fills(exec_ts)"),
    ("ix_orders_signal_id", "CREATE INDEX IF NOT EXISTS ix_orders_signal_id ON orders(signal_id)"),
    ("ix_orders_ib_order_id", "CREATE INDEX IF NOT EXISTS ix_orders_ib_order_id ON orders(ib_order_id)"),
    ("ix_risk_decisions_signal_id",
     "CREATE INDEX IF NOT EXISTS ix_risk_decisions_signal_id ON risk_decisions(signal_id)"),
    ("ix_signals_ts", "CREATE INDEX IF NOT EXISTS ix_signals_ts ON signals(ts)"),
)


def _ensure_indices(conn: sqlite3.Connection) -> None:
    for _name, ddl in INDICES:
        conn.execute(ddl)
    conn.commit()


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    """ALTER TABLE ADD COLUMN, tolerating a column SCHEMA already created.

    Keeps migrations runnable against both an old database (column absent,
    added here) and a freshly created one (column already present from
    SCHEMA), so the two converge on an identical shape."""
    if column not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _m1_fill_identity_and_indices(conn: sqlite3.Connection) -> None:
    """exec_id/exec_ts on fills, symbol on orders, and the missing indices.

    All additive (ALTER TABLE ADD COLUMN and CREATE INDEX are cheap and do
    not rewrite the table), so this is safe to apply while the bot runs.

    exec_id deliberately has no backfill: a SQLite UNIQUE index treats NULLs
    as distinct, so existing rows stay NULL and only post-migration fills are
    deduped at write time.
    """
    _add_column_if_missing(conn, "fills", "exec_id", "TEXT")
    _add_column_if_missing(conn, "fills", "exec_ts", "TEXT")
    _add_column_if_missing(conn, "orders", "symbol", "TEXT")
    # Indices are applied by _ensure_indices after all migrations run.


def _m2_orders_signal_id_nullable(conn: sqlite3.Connection) -> None:
    """Rebuilds `orders` with a NULLABLE signal_id.

    This is the change that lets an exit be recorded at all when the bot
    cannot attribute it to an originating signal. With signal_id NOT NULL,
    main._journal_flatten_fill had no choice but to early-return -- which is
    why only ~45% of traded notional has a journaled exit, why there are 2
    emergency_flatten rows in six weeks, and why BKYI/CNTB/VBIO read as open
    positions after they had demonstrably been flattened.

    SQLite cannot drop a NOT NULL constraint with ALTER, so the table is
    rebuilt. Callers should stop the bot first (see run_migrations' docstring)
    -- this is the one migration that rewrites an existing table.
    """
    already_nullable = not any(
        row[1] == "signal_id" and row[3] for row in conn.execute("PRAGMA table_info(orders)")
    )
    if already_nullable:
        return  # freshly created from SCHEMA, which already declares it nullable
    conn.execute(
        """
        CREATE TABLE orders_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id INTEGER REFERENCES signals(id),
            symbol TEXT,
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
        )
        """
    )
    conn.execute(
        """
        INSERT INTO orders_new
            (id, signal_id, symbol, ib_order_id, role, action, qty, order_type,
             limit_price, stop_price, oca_group, status, ts_submitted, ts_last_update)
        SELECT id, signal_id, symbol, ib_order_id, role, action, qty, order_type,
               limit_price, stop_price, oca_group, status, ts_submitted, ts_last_update
        FROM orders
        """
    )
    conn.execute("DROP TABLE orders")
    conn.execute("ALTER TABLE orders_new RENAME TO orders")
    # DROP TABLE took this table's indices with it; _ensure_indices recreates
    # them once every migration has run.


MIGRATIONS: list[tuple[int, str, object]] = [
    (1, "fills.exec_id/exec_ts, orders.symbol, hot-path indices", _m1_fill_identity_and_indices),
    (2, "orders.signal_id nullable (rebuild)", _m2_orders_signal_id_nullable),
]

# Migrations that rewrite an existing table rather than only adding to it.
# run_migrations refuses these unless explicitly allowed, so a routine bot
# start can never rebuild a table out from under itself.
_REWRITING_MIGRATIONS = {2}

LATEST_SCHEMA_VERSION = max(v for v, _, _ in MIGRATIONS)


def run_migrations(conn: sqlite3.Connection, allow_rewrites: bool = False) -> list[int]:
    """Applies any migrations newer than PRAGMA user_version, in order.

    Returns the versions applied. Additive migrations run automatically on
    every startup; a migration listed in _REWRITING_MIGRATIONS is skipped
    unless `allow_rewrites` is set, because rebuilding a table while the bot
    is live risks losing writes. Run those via scripts/migrate_journal.py
    with the bot stopped.
    """
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    applied: list[int] = []
    for version, description, migrate in MIGRATIONS:
        if version <= current:
            continue
        if version in _REWRITING_MIGRATIONS and not allow_rewrites:
            logger.warning(
                "Journal migration %d (%s) rewrites a table and is pending -- "
                "run scripts/migrate_journal.py with the bot stopped to apply it",
                version,
                description,
            )
            break  # later migrations may depend on it; do not skip ahead
        logger.info("Applying journal migration %d: %s", version, description)
        # foreign_keys must be toggled OUTSIDE a transaction to take effect.
        had_fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        if version in _REWRITING_MIGRATIONS:
            conn.execute("PRAGMA foreign_keys = OFF")
        try:
            with conn:  # BEGIN/COMMIT, rolls back on exception
                migrate(conn)
                conn.execute(f"PRAGMA user_version = {version}")
            if version in _REWRITING_MIGRATIONS:
                violations = conn.execute("PRAGMA foreign_key_check").fetchall()
                if violations:
                    raise RuntimeError(f"migration {version} left FK violations: {violations[:5]}")
        finally:
            if had_fk:
                conn.execute("PRAGMA foreign_keys = ON")
        applied.append(version)
    return applied


def get_connection(db_path: Path, allow_rewrites: bool = False) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    # WAL so the ops dashboard's polling reader doesn't block against the
    # bot's writer (rollback-journal mode blocks readers outright, and with
    # no busy_timeout a reader raises "database is locked" immediately
    # rather than waiting). synchronous=NORMAL is the standard, safe
    # companion to WAL.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")

    # A database created fresh from SCHEMA is already at the latest shape, so
    # stamp it rather than running migrations against it -- otherwise the
    # rewrite-gated migration 2 would be reported as "pending" forever on a
    # brand-new database that never needed it.
    is_fresh = (
        conn.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='orders'").fetchone()[0] == 0
    )
    conn.executescript(SCHEMA)
    conn.commit()

    if is_fresh:
        conn.execute(f"PRAGMA user_version = {LATEST_SCHEMA_VERSION}")
        conn.commit()
    else:
        run_migrations(conn, allow_rewrites=allow_rewrites)
    _ensure_indices(conn)
    return conn
