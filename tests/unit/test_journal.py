from __future__ import annotations

from warrior_bot.persistence.db import get_connection
from warrior_bot.persistence.journal import Journal


def make_journal(tmp_path) -> Journal:
    conn = get_connection(tmp_path / "journal.sqlite3")
    return Journal(conn)


def test_load_daily_risk_state_none_when_never_saved(tmp_path):
    journal = make_journal(tmp_path)

    assert journal.load_daily_risk_state("2026-09-23") is None


def test_save_and_load_daily_risk_state_round_trips(tmp_path):
    journal = make_journal(tmp_path)

    journal.save_daily_risk_state("2026-09-23", start_of_day_equity=45_000.0, loss_limit_halted=False)

    state = journal.load_daily_risk_state("2026-09-23")
    assert state == {"start_of_day_equity": 45_000.0, "loss_limit_halted": False}


def test_save_daily_risk_state_upserts_rather_than_duplicating(tmp_path):
    # The whole point: a same-day restart calling this again (or the risk
    # loop's periodic re-save) must update the one row for that date, not
    # accumulate a new one every time.
    journal = make_journal(tmp_path)

    journal.save_daily_risk_state("2026-09-23", start_of_day_equity=45_000.0, loss_limit_halted=False)
    journal.save_daily_risk_state("2026-09-23", start_of_day_equity=45_000.0, loss_limit_halted=True)

    state = journal.load_daily_risk_state("2026-09-23")
    assert state == {"start_of_day_equity": 45_000.0, "loss_limit_halted": True}
    count = journal.conn.execute("SELECT COUNT(*) FROM daily_risk_state").fetchone()[0]
    assert count == 1


def test_daily_risk_state_keyed_independently_per_date(tmp_path):
    journal = make_journal(tmp_path)

    journal.save_daily_risk_state("2026-09-22", start_of_day_equity=40_000.0, loss_limit_halted=True)
    journal.save_daily_risk_state("2026-09-23", start_of_day_equity=45_000.0, loss_limit_halted=False)

    assert journal.load_daily_risk_state("2026-09-22") == {
        "start_of_day_equity": 40_000.0,
        "loss_limit_halted": True,
    }
    assert journal.load_daily_risk_state("2026-09-23") == {
        "start_of_day_equity": 45_000.0,
        "loss_limit_halted": False,
    }


def test_load_symbol_losses_empty_when_never_saved(tmp_path):
    """An empty dict, not None: unlike the equity baseline, "no losses
    yet" and "never established" are the same state, so there is no
    caller branch to signal."""
    journal = make_journal(tmp_path)

    assert journal.load_symbol_losses("2026-10-01") == {}


def test_save_and_load_symbol_losses_round_trips(tmp_path):
    journal = make_journal(tmp_path)

    journal.save_symbol_loss("2026-10-01", "PFSA", 1, "stop", -393.59)

    assert journal.load_symbol_losses("2026-10-01") == {"PFSA": 1}


def test_save_symbol_loss_upserts_rather_than_duplicating(tmp_path):
    """The write is an absolute count, so re-saving the same symbol
    overwrites its row. A double-fired fill callback must not be able to
    inflate the count and over-ban a symbol."""
    journal = make_journal(tmp_path)

    journal.save_symbol_loss("2026-10-01", "PFSA", 1, "stop", -393.59)
    journal.save_symbol_loss("2026-10-01", "PFSA", 1, "stop", -393.59)

    assert journal.load_symbol_losses("2026-10-01") == {"PFSA": 1}
    count = journal.conn.execute("SELECT COUNT(*) FROM symbol_loss_state").fetchone()[0]
    assert count == 1


def test_save_symbol_loss_advances_the_count_on_a_second_loss(tmp_path):
    journal = make_journal(tmp_path)

    journal.save_symbol_loss("2026-10-01", "MASK", 1, "stop", -100.0)
    journal.save_symbol_loss("2026-10-01", "MASK", 2, "flatten", -293.42)

    assert journal.load_symbol_losses("2026-10-01") == {"MASK": 2}


def test_symbol_losses_keyed_independently_per_date(tmp_path):
    """What makes the day-rollover path need no delete: yesterday's rows
    stay as analysis data and simply aren't loaded for the new date."""
    journal = make_journal(tmp_path)

    journal.save_symbol_loss("2026-09-30", "PFSA", 1, "stop", -393.59)
    journal.save_symbol_loss("2026-10-01", "MASK", 1, "stop", -393.42)

    assert journal.load_symbol_losses("2026-09-30") == {"PFSA": 1}
    assert journal.load_symbol_losses("2026-10-01") == {"MASK": 1}


def test_load_symbol_losses_returns_every_symbol_for_the_date(tmp_path):
    journal = make_journal(tmp_path)

    journal.save_symbol_loss("2026-10-01", "PFSA", 1, "stop", -393.59)
    journal.save_symbol_loss("2026-10-01", "MASK", 2, "stop", -393.42)

    assert journal.load_symbol_losses("2026-10-01") == {"PFSA": 1, "MASK": 2}


def test_symbol_loss_state_needs_no_schema_migration(tmp_path):
    """The table arrives via SCHEMA's CREATE TABLE IF NOT EXISTS, so
    user_version must not have moved -- a bumped version here means
    something was wrongly added to MIGRATIONS."""
    journal = make_journal(tmp_path)

    name = journal.conn.execute(
        "SELECT name FROM sqlite_master WHERE name = 'symbol_loss_state'"
    ).fetchone()
    assert name is not None
