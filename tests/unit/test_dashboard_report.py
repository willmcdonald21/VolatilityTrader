from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from warrior_bot.persistence.db import get_connection
from warrior_bot.utils.time_utils import EASTERN

from scripts.dashboard_report import build_report, dedup_stop_fills, et_day_utc_bounds

DAY = datetime(2026, 9, 14, tzinfo=EASTERN).date()


def _make_rows(conn: sqlite3.Connection, specs: list[dict]) -> list[sqlite3.Row]:
    """Builds real sqlite3.Row objects (dedup_stop_fills needs genuine Row
    dict-style access, not a hand-rolled stand-in) from a throwaway table
    shaped like the join projection dedup_stop_fills actually receives."""
    conn.execute(
        "CREATE TABLE t (order_id INTEGER, role TEXT, fill_qty REAL, fill_price REAL, fill_ts TEXT)"
    )
    conn.executemany(
        "INSERT INTO t VALUES (:order_id, :role, :fill_qty, :fill_price, :fill_ts)", specs
    )
    conn.commit()
    conn.row_factory = sqlite3.Row
    return conn.execute("SELECT * FROM t ORDER BY rowid").fetchall()


def test_dedup_drops_exact_back_to_back_stop_duplicate():
    conn = sqlite3.connect(":memory:")
    t0 = "2026-09-14T14:00:00.000000+00:00"
    t1 = "2026-09-14T14:00:00.050000+00:00"  # 50ms later -- the real bug's signature
    rows = _make_rows(
        conn,
        [
            {"order_id": 1, "role": "stop", "fill_qty": 100.0, "fill_price": 9.0, "fill_ts": t0},
            {"order_id": 1, "role": "stop", "fill_qty": 100.0, "fill_price": 9.0, "fill_ts": t1},
        ],
    )
    result = dedup_stop_fills(rows)
    assert len(result) == 1


def test_dedup_keeps_two_genuine_separate_stop_fills():
    conn = sqlite3.connect(":memory:")
    t0 = "2026-09-14T14:00:00+00:00"
    t1 = "2026-09-14T14:05:00+00:00"  # 5 minutes later -- a real second partial fill
    rows = _make_rows(
        conn,
        [
            {"order_id": 1, "role": "stop", "fill_qty": 100.0, "fill_price": 9.0, "fill_ts": t0},
            {"order_id": 1, "role": "stop", "fill_qty": 100.0, "fill_price": 9.0, "fill_ts": t1},
        ],
    )
    result = dedup_stop_fills(rows)
    assert len(result) == 2


def test_dedup_ignores_non_stop_roles():
    # parent/target/scale_out were never affected by the bug -- an
    # identical-looking pair there must be left alone even if it happens
    # to match the stop pattern by coincidence.
    conn = sqlite3.connect(":memory:")
    t0 = "2026-09-14T14:00:00.000000+00:00"
    t1 = "2026-09-14T14:00:00.050000+00:00"
    rows = _make_rows(
        conn,
        [
            {"order_id": 1, "role": "parent", "fill_qty": 100.0, "fill_price": 9.0, "fill_ts": t0},
            {"order_id": 1, "role": "parent", "fill_qty": 100.0, "fill_price": 9.0, "fill_ts": t1},
        ],
    )
    result = dedup_stop_fills(rows)
    assert len(result) == 2


def test_et_day_utc_bounds_spans_midnight_to_midnight_et():
    start, end = et_day_utc_bounds(DAY)
    # 2026-09-14 is EDT (UTC-4): midnight ET == 04:00 UTC
    assert start == "2026-09-14T04:00:00+00:00"
    assert end == "2026-09-15T04:00:00+00:00"


# -- build_report: full pipeline against a real schema'd journal --


def _seed_signal(conn, *, symbol, strategy="gap_and_go", entry=10.0, stop=9.5, target=10.5, qty=100, ts=None):
    ts = ts or "2026-09-14T14:00:00+00:00"
    cur = conn.execute(
        "INSERT INTO signals (ts, symbol, strategy, side, entry_price, stop_price, target_price) "
        "VALUES (?, ?, ?, 'BUY', ?, ?, ?)",
        (ts, symbol, strategy, entry, stop, target),
    )
    signal_id = cur.lastrowid
    conn.execute(
        "INSERT INTO risk_decisions (signal_id, ts, decision, reason, sized_qty) VALUES (?, ?, 'accepted', 'accepted', ?)",
        (signal_id, ts, qty),
    )
    return signal_id


def _seed_order(conn, signal_id, *, role, action, qty):
    cur = conn.execute(
        "INSERT INTO orders (signal_id, ib_order_id, role, action, qty, order_type, ts_submitted) "
        "VALUES (?, ?, ?, ?, ?, 'LMT', '2026-09-14T14:00:00+00:00')",
        (signal_id, signal_id * 10, role, action, qty),
    )
    return cur.lastrowid


def _seed_fill(conn, order_id, *, qty, price, ts, commission=0.0):
    conn.execute(
        "INSERT INTO fills (order_id, ib_order_id, ts, fill_qty, fill_price, commission, realized_pnl) "
        "VALUES (?, ?, ?, ?, ?, ?, NULL)",
        (order_id, order_id, ts, qty, price, commission),
    )


def test_build_report_computes_pnl_from_fill_prices_not_realized_pnl_field(tmp_path):
    conn = get_connection(tmp_path / "journal.sqlite3")
    sid = _seed_signal(conn, symbol="TEST", entry=10.0, stop=9.5, qty=100)
    parent = _seed_order(conn, sid, role="parent", action="BUY", qty=100)
    stop = _seed_order(conn, sid, role="stop", action="SELL", qty=100)
    _seed_fill(conn, parent, qty=100, price=10.0, ts="2026-09-14T14:00:01+00:00")
    # realized_pnl column is left NULL above -- if build_report trusted it
    # instead of computing from fill prices, this trade would show 0 P&L.
    _seed_fill(conn, stop, qty=100, price=10.5, ts="2026-09-14T14:10:00+00:00")
    conn.commit()

    report = build_report(conn, DAY)

    assert report["summary"]["trade_count"] == 1
    trade = report["trades"][0]
    assert trade["avg_entry"] == 10.0
    assert trade["avg_exit"] == 10.5
    assert trade["still_open"] is False
    assert trade["realized_pnl_est"] == 50.0
    assert trade["net_pnl"] == 50.0


def test_build_report_flags_partial_exit_as_still_open(tmp_path):
    conn = get_connection(tmp_path / "journal.sqlite3")
    sid = _seed_signal(conn, symbol="PART", qty=100)
    parent = _seed_order(conn, sid, role="parent", action="BUY", qty=100)
    scale_out = _seed_order(conn, sid, role="scale_out", action="SELL", qty=40)
    _seed_fill(conn, parent, qty=100, price=10.0, ts="2026-09-14T14:00:01+00:00")
    _seed_fill(conn, scale_out, qty=40, price=10.2, ts="2026-09-14T14:05:00+00:00")
    conn.commit()

    report = build_report(conn, DAY)

    trade = report["trades"][0]
    assert trade["entry_qty"] == 100
    assert trade["exit_qty"] == 40
    assert trade["still_open"] is True
    assert trade["realized_pnl_est"] == 8.0  # (10.2-10.0)*40
    assert report["summary"]["open_count"] == 1
    assert report["summary"]["closed_count"] == 0


def test_build_report_never_filled_signal_has_no_entry_price(tmp_path):
    conn = get_connection(tmp_path / "journal.sqlite3")
    sid = _seed_signal(conn, symbol="NOFILL", qty=100)
    _seed_order(conn, sid, role="parent", action="BUY", qty=100)  # no fills at all
    conn.commit()

    report = build_report(conn, DAY)

    trade = report["trades"][0]
    assert trade["avg_entry"] is None
    assert trade["entry_qty"] == 0
    assert trade["still_open"] is True
    assert trade["realized_pnl_est"] == 0.0


def test_build_report_subtracts_commission_from_net_pnl(tmp_path):
    conn = get_connection(tmp_path / "journal.sqlite3")
    sid = _seed_signal(conn, symbol="FEE", qty=100)
    parent = _seed_order(conn, sid, role="parent", action="BUY", qty=100)
    stop = _seed_order(conn, sid, role="stop", action="SELL", qty=100)
    _seed_fill(conn, parent, qty=100, price=10.0, ts="2026-09-14T14:00:01+00:00", commission=1.0)
    _seed_fill(conn, stop, qty=100, price=10.5, ts="2026-09-14T14:10:00+00:00", commission=1.0)
    conn.commit()

    report = build_report(conn, DAY)

    trade = report["trades"][0]
    assert trade["realized_pnl_est"] == 50.0
    assert trade["commission_total"] == 2.0
    assert trade["net_pnl"] == 48.0


def test_build_report_excludes_signals_outside_the_requested_day(tmp_path):
    conn = get_connection(tmp_path / "journal.sqlite3")
    _seed_signal(conn, symbol="YESTERDAY", qty=100, ts="2026-09-13T14:00:00+00:00")
    _seed_signal(conn, symbol="TODAY", qty=100, ts="2026-09-14T14:00:00+00:00")
    conn.commit()

    report = build_report(conn, DAY)

    symbols = [t["symbol"] for t in report["trades"]]
    assert symbols == ["TODAY"]


def test_build_report_flags_naked_short_overshoot_as_open_not_a_clean_win(tmp_path):
    # Reproduces the exact live incident (GVH, 2026-09-14): a stop-sizing
    # bug let a replacement stop sell more shares than the entry ever
    # bought, leaving a naked short. exit_qty (5384) > entry_qty (3943)
    # must NOT read as "closed flat" just because it's not *less* than
    # entry_qty -- and the realized P&L must price only the 3943 shares
    # that genuinely round-tripped, not all 5384.
    conn = get_connection(tmp_path / "journal.sqlite3")
    sid = _seed_signal(conn, symbol="GVH", entry=1.0682, stop=1.05, qty=3943)
    parent = _seed_order(conn, sid, role="parent", action="BUY", qty=3943)
    stop = _seed_order(conn, sid, role="stop", action="SELL", qty=5384)
    _seed_fill(conn, parent, qty=3943, price=1.0682, ts="2026-09-14T14:58:40+00:00")
    _seed_fill(conn, stop, qty=5384, price=1.1284, ts="2026-09-14T15:15:24+00:00")
    conn.commit()

    report = build_report(conn, DAY)

    trade = report["trades"][0]
    assert trade["entry_qty"] == 3943
    assert trade["exit_qty"] == 5384
    assert trade["still_open"] is True  # flagged, not a clean closed win
    # (1.1284 - 1.0682) * 3943 -- only the round-tripped 3943 shares priced in,
    # not all 5384
    assert trade["realized_pnl_est"] == round((1.1284 - 1.0682) * 3943, 2)
    assert report["summary"]["open_count"] == 1
    assert report["summary"]["closed_count"] == 0


def test_build_report_dedups_pre_fix_duplicate_stop_fills(tmp_path):
    # Reproduces the exact live incident: a stop fill journaled twice via
    # two listeners on the same IBKR fill event, milliseconds apart.
    conn = get_connection(tmp_path / "journal.sqlite3")
    sid = _seed_signal(conn, symbol="DUPBUG", qty=100)
    parent = _seed_order(conn, sid, role="parent", action="BUY", qty=100)
    stop = _seed_order(conn, sid, role="stop", action="SELL", qty=100)
    _seed_fill(conn, parent, qty=100, price=10.0, ts="2026-09-14T14:00:01+00:00")
    _seed_fill(conn, stop, qty=100, price=10.5, ts="2026-09-14T14:10:00.000000+00:00")
    _seed_fill(conn, stop, qty=100, price=10.5, ts="2026-09-14T14:10:00.040000+00:00")  # duplicate
    conn.commit()

    report = build_report(conn, DAY)

    trade = report["trades"][0]
    assert trade["exit_qty"] == 100  # not 200 -- the duplicate is dropped
    assert trade["still_open"] is False
    assert trade["realized_pnl_est"] == 50.0
