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
