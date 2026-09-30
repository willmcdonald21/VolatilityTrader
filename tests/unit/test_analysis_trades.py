from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone

from warrior_bot.analysis.trades import (
    coverage,
    et_day_utc_bounds,
    in_et_day,
    reconstruct_trades,
)
from warrior_bot.persistence.db import get_connection

BASE = datetime(2026, 9, 20, 14, 0, tzinfo=timezone.utc)


def _db(tmp_path):
    return get_connection(tmp_path / "j.sqlite3")


def _signal(conn, sid, symbol="AAA", strategy="bull_flag", ts=None, accepted=True, qty=100):
    conn.execute(
        "INSERT INTO signals (id, ts, symbol, strategy, side, entry_price, stop_price, target_price, context_json) "
        "VALUES (?,?,?,?,'BUY',10.0,9.0,12.0,NULL)",
        (sid, (ts or BASE).isoformat(), symbol, strategy),
    )
    conn.execute(
        "INSERT INTO risk_decisions (signal_id, ts, decision, reason, sized_qty) VALUES (?,?,?,?,?)",
        (sid, (ts or BASE).isoformat(), "accepted" if accepted else "rejected", "x", qty),
    )
    conn.commit()


def _order(conn, oid, sid, role, action, symbol="AAA"):
    conn.execute(
        "INSERT INTO orders (id, signal_id, symbol, ib_order_id, role, action, qty, order_type, status, ts_submitted) "
        "VALUES (?,?,?,?,?,?,100,'LMT','Filled',?)",
        (oid, sid, symbol, oid, role, action, BASE.isoformat()),
    )
    conn.commit()


def _fill(conn, oid, qty, price, exec_id=None, minutes=0):
    stamp = (BASE + timedelta(minutes=minutes)).isoformat()
    conn.execute(
        "INSERT INTO fills (order_id, ib_order_id, ts, exec_id, exec_ts, fill_qty, fill_price) "
        "VALUES (?,?,?,?,?,?,?)",
        (oid, oid, stamp, exec_id, stamp, qty, price),
    )
    conn.commit()


def test_a_clean_round_trip_is_closed(tmp_path):
    conn = _db(tmp_path)
    _signal(conn, 1)
    _order(conn, 10, 1, "parent", "BUY")
    _order(conn, 11, 1, "stop", "SELL")
    _fill(conn, 10, 100, 10.0, "e1")
    _fill(conn, 11, 100, 11.0, "e2", minutes=5)

    t = reconstruct_trades(conn)[0]

    assert t.status == "closed"
    assert t.is_settled
    assert t.avg_entry == 10.0 and t.avg_exit == 11.0
    assert t.gross_pnl == 100.0
    assert t.duration_minutes == 5.0
    assert t.duration_source == "exec_ts"


def test_partial_close_is_not_scored_as_settled(tmp_path):
    # win_rate_analysis used to count these as settled wins/losses while
    # dashboard_report called them open -- the 13% disagreement.
    conn = _db(tmp_path)
    _signal(conn, 1)
    _order(conn, 10, 1, "parent", "BUY")
    _order(conn, 11, 1, "scale_out", "SELL")
    _fill(conn, 10, 100, 10.0, "e1")
    _fill(conn, 11, 40, 11.0, "e2", minutes=3)

    t = reconstruct_trades(conn)[0]

    assert t.status == "partial"
    assert not t.is_settled
    assert t.closed_qty == 40


def test_no_exit_reads_as_open(tmp_path):
    conn = _db(tmp_path)
    _signal(conn, 1)
    _order(conn, 10, 1, "parent", "BUY")
    _fill(conn, 10, 100, 10.0, "e1")

    assert reconstruct_trades(conn)[0].status == "open"


def test_oversold_is_flagged_not_booked_as_profit(tmp_path):
    # The 2026-09-16 NRXS shape: more sold than bought. Pricing the excess
    # at avg_exit would book an open short as locked-in profit.
    conn = _db(tmp_path)
    _signal(conn, 1)
    _order(conn, 10, 1, "parent", "BUY")
    _order(conn, 11, 1, "stop", "SELL")
    _fill(conn, 10, 100, 10.0, "e1")
    _fill(conn, 11, 150, 11.0, "e2", minutes=2)

    t = reconstruct_trades(conn)[0]

    assert t.status == "oversold"
    assert not t.is_settled
    assert t.closed_qty == 100  # only the round-tripped portion


def test_accepted_but_unfilled_is_never_filled(tmp_path):
    conn = _db(tmp_path)
    _signal(conn, 1)
    _order(conn, 10, 1, "parent", "BUY")

    assert reconstruct_trades(conn)[0].status == "never_filled"


def test_unattributed_flatten_rows_do_not_crash_reconstruction(tmp_path):
    # signal_id is nullable now; such a row has no signal to join to and
    # must simply not appear, rather than breaking the report.
    conn = _db(tmp_path)
    _signal(conn, 1)
    _order(conn, 10, 1, "parent", "BUY")
    _fill(conn, 10, 100, 10.0, "e1")
    conn.execute(
        "INSERT INTO orders (id, signal_id, symbol, ib_order_id, role, action, qty, order_type, status, ts_submitted) "
        "VALUES (99, NULL, 'GHOST', 99, 'emergency_flatten', 'SELL', 50, 'MKT', 'Filled', ?)",
        (BASE.isoformat(),),
    )
    conn.commit()
    _fill(conn, 99, 50, 3.0, "e-ghost")

    trades = reconstruct_trades(conn)

    assert len(trades) == 1 and trades[0].signal_id == 1


def test_coverage_reports_missing_exits_and_zero_commissions(tmp_path):
    conn = _db(tmp_path)
    _signal(conn, 1)
    _order(conn, 10, 1, "parent", "BUY")
    _fill(conn, 10, 100, 10.0, "e1")  # bought, never sold

    cov = coverage(reconstruct_trades(conn))

    assert cov.exit_coverage_pct == 0.0
    assert cov.commissions_all_zero is True
    text = " ".join(cov.caveats())
    assert "journaled exit" in text
    assert "GROSS" in text


def test_duration_prefers_exec_ts_and_says_when_it_could_not(tmp_path):
    conn = _db(tmp_path)
    _signal(conn, 1)
    _order(conn, 10, 1, "parent", "BUY")
    _order(conn, 11, 1, "stop", "SELL")
    # Legacy rows: no exec_ts, so duration falls back to write time.
    stamp = BASE.isoformat()
    conn.execute(
        "INSERT INTO fills (order_id, ib_order_id, ts, fill_qty, fill_price) VALUES (10,10,?,100,10.0)", (stamp,)
    )
    conn.execute(
        "INSERT INTO fills (order_id, ib_order_id, ts, fill_qty, fill_price) VALUES (11,11,?,100,11.0)",
        ((BASE + timedelta(minutes=9)).isoformat(),),
    )
    conn.commit()

    t = reconstruct_trades(conn)[0]

    assert t.duration_source == "write_ts"
    cov = coverage([t])
    assert cov.duration_from_exec_ts_pct == 0.0
    assert any("execution times" in c for c in cov.caveats())


def test_et_day_bounds_convert_rather_than_compare_strings(tmp_path):
    # A trade at 20:00 ET on the 24th is 2026-09-25T00:00Z -- comparing an
    # ET date against a UTC string directly put it on the wrong day.
    start, end = et_day_utc_bounds(date(2026, 9, 24))
    assert start == "2026-09-24T04:00:00+00:00"
    assert end == "2026-09-25T04:00:00+00:00"

    conn = _db(tmp_path)
    evening_et = datetime(2026, 9, 25, 0, 30, tzinfo=timezone.utc)  # 20:30 ET on the 24th
    _signal(conn, 1, ts=evening_et)
    _order(conn, 10, 1, "parent", "BUY")
    _fill(conn, 10, 100, 10.0, "e1")

    trades = reconstruct_trades(conn)
    assert len(in_et_day(trades, date(2026, 9, 24))) == 1
    assert len(in_et_day(trades, date(2026, 9, 25))) == 0


def test_duplicate_exec_id_cannot_double_count(tmp_path):
    conn = _db(tmp_path)
    _signal(conn, 1)
    _order(conn, 10, 1, "parent", "BUY")
    _fill(conn, 10, 100, 10.0, "same-exec")
    try:
        _fill(conn, 10, 100, 10.0, "same-exec")  # a re-delivered execution
    except sqlite3.IntegrityError:
        pass  # plain INSERT raises; the journal uses INSERT OR IGNORE

    assert reconstruct_trades(conn)[0].entry_qty == 100
