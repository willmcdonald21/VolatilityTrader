"""Applies pending journal migrations, including the ones that rewrite a
table. STOP THE BOT FIRST.

Additive migrations (new columns, new indices) run automatically whenever
anything opens the journal. This script exists for the ones that rebuild an
existing table -- SQLite cannot drop a NOT NULL constraint with ALTER, so
`orders` has to be recreated and copied, which must not happen underneath a
live writer.

It takes a timestamped backup first, verifies row counts across every table
before and after, and runs foreign_key_check and integrity_check. If
anything looks wrong it says so and leaves the backup in place.

    python scripts/migrate_journal.py            # apply
    python scripts/migrate_journal.py --dry-run  # rehearse on a copy only
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from warrior_bot.config import load_config
from warrior_bot.persistence.db import LATEST_SCHEMA_VERSION, get_connection


def copy_database(src: Path, dst: Path) -> None:
    """A CONSISTENT copy, via SQLite's own backup API.

    Not shutil.copy2: the journal runs in WAL mode, so recently committed
    rows live in the `-wal` sidecar until a checkpoint. Copying just the
    .sqlite3 file silently loses them -- the dry run caught exactly that,
    producing a "backup" 4 heartbeat rows short of the original. A backup
    that quietly drops recent writes is worse than none at all.
    """
    src_conn = sqlite3.connect(str(src))
    dst_conn = sqlite3.connect(str(dst))
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()

TABLES = (
    "signals",
    "risk_decisions",
    "orders",
    "fills",
    "rejections",
    "account_snapshots",
    "bot_heartbeat",
    "kill_switch_events",
    "daily_risk_state",
    "symbol_loss_state",
)


def _counts(db_path: Path) -> dict[str, int]:
    conn = sqlite3.connect(str(db_path))
    try:
        out = {}
        for table in TABLES:
            try:
                out[table] = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            except sqlite3.OperationalError:
                out[table] = 0  # table doesn't exist yet on an older DB
        return out
    finally:
        conn.close()


def _bot_is_running() -> bool:
    """Best-effort check so this can't be run against a live writer."""
    try:
        import subprocess

        result = subprocess.run(
            ["pgrep", "-f", "python -m warrior_bot.main"], capture_output=True, text=True
        )
        return bool(result.stdout.strip())
    except Exception:
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Rehearse on a copy; never touch the real file")
    parser.add_argument("--force", action="store_true", help="Proceed even if the bot appears to be running")
    args = parser.parse_args()

    config = load_config()
    db_path = config.resolve_path(config.journal.db_path)
    if not db_path.exists():
        print(f"No journal at {db_path} -- nothing to migrate.")
        return

    # A dry run copies the file and works on the copy, so a live writer is
    # irrelevant to it.
    if _bot_is_running() and not args.force and not args.dry_run:
        print("The bot appears to be RUNNING. Rebuilding a table under a live writer risks losing rows.")
        print("Stop it first (kill the `python -m warrior_bot.main` process), or pass --force.")
        sys.exit(1)

    current = sqlite3.connect(str(db_path)).execute("PRAGMA user_version").fetchone()[0]
    print(f"journal:        {db_path}")
    print(f"schema version: {current} -> {LATEST_SCHEMA_VERSION}")
    if current >= LATEST_SCHEMA_VERSION:
        print("Already up to date.")
        return

    before = _counts(db_path)
    print(f"before:         {before}")

    if args.dry_run:
        target = db_path.with_suffix(f".dryrun-{datetime.now():%Y%m%d-%H%M%S}.sqlite3")
        copy_database(db_path, target)
        print(f"\n[dry run] rehearsing on {target}")
    else:
        backup = db_path.with_suffix(f".backup-{datetime.now():%Y%m%d-%H%M%S}.sqlite3")
        copy_database(db_path, backup)
        print(f"backup:         {backup}")
        target = db_path

    conn = get_connection(target, allow_rewrites=True)

    after = _counts(target)
    print(f"after:          {after}")

    problems = []
    if before != after:
        problems.append(f"ROW COUNTS CHANGED: {before} -> {after}")
    fk = conn.execute("PRAGMA foreign_key_check").fetchall()
    if fk:
        problems.append(f"foreign key violations: {fk[:5]}")
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        problems.append(f"integrity_check: {integrity}")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version != LATEST_SCHEMA_VERSION:
        problems.append(f"version is {version}, expected {LATEST_SCHEMA_VERSION}")
    nullable = not [r[3] for r in conn.execute("PRAGMA table_info(orders)") if r[1] == "signal_id"][0]
    if not nullable:
        problems.append("orders.signal_id is still NOT NULL")

    print()
    if problems:
        for p in problems:
            print(f"  FAILED: {p}")
        if not args.dry_run:
            print(f"\nThe backup is at {backup} -- restore it by copying back over {db_path}.")
        sys.exit(1)

    print("  row counts identical")
    print("  foreign_key_check clean")
    print("  integrity_check ok")
    print(f"  orders.signal_id nullable, version {version}")
    print("\nMigration complete." if not args.dry_run else "\nDry run complete -- the real journal was not touched.")


if __name__ == "__main__":
    main()
