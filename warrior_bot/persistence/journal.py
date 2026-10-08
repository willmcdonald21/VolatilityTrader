from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from warrior_bot.risk.account_state import AccountSnapshot
from warrior_bot.risk.risk_manager import RiskDecision
from warrior_bot.signals.signal import Signal

logger = logging.getLogger("warrior_bot.persistence.journal")

# ib_async's Order declares lmtPrice/auxPrice defaulting to UNSET_DOUBLE
# (~1.797e308) and the attributes always exist, so `getattr(order, ..., None)`
# returns the sentinel rather than None. Anything at/above this is not a price.
_UNSET_DOUBLE_FLOOR = 1e15


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _real_price(value: float | None) -> float | None:
    """None for a missing price, including IBKR's UNSET_DOUBLE sentinel."""
    if value is None:
        return None
    try:
        if abs(float(value)) >= _UNSET_DOUBLE_FLOOR:
            return None
    except (TypeError, ValueError):
        return None
    return value


def _iso(value) -> str | None:
    """IBKR execution timestamps arrive as datetimes; store them as ISO."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return str(value)


class Journal:
    """Every signal, risk decision, order, fill, and rejection is written
    here — this is the feedback loop for tuning strategy parameters and for
    judging whether the bot is actually replicating the intended edge."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def record_signal(self, signal: Signal) -> int:
        cur = self.conn.execute(
            """INSERT INTO signals (ts, symbol, strategy, side, entry_price, stop_price, target_price, context_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                signal.ts.isoformat(),
                signal.symbol,
                signal.strategy,
                signal.side,
                signal.entry_price,
                signal.stop_price,
                signal.target_price,
                json.dumps(signal.context),
            ),
        )
        self.conn.commit()
        return cur.lastrowid

    def record_risk_decision(self, signal_id: int, decision: RiskDecision) -> int:
        cur = self.conn.execute(
            """INSERT INTO risk_decisions
               (signal_id, ts, decision, reason, sized_qty, equity_snapshot, daily_pnl_snapshot, open_positions_count)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                signal_id,
                _now(),
                "accepted" if decision.accepted else "rejected",
                decision.reason,
                decision.sized_qty,
                decision.snapshot.net_liquidation,
                decision.snapshot.daily_realized_pnl,
                decision.snapshot.open_positions_count,
            ),
        )
        self.conn.commit()
        return cur.lastrowid

    def record_rejection(self, signal: Signal, reason: str, detail: dict | None = None) -> int:
        cur = self.conn.execute(
            """INSERT INTO rejections (ts, symbol, strategy, reason, detail_json) VALUES (?, ?, ?, ?, ?)""",
            (_now(), signal.symbol, signal.strategy, reason, json.dumps(detail or {})),
        )
        self.conn.commit()
        return cur.lastrowid

    def record_order(
        self,
        signal_id: int | None,
        ib_order_id: int,
        role: str,
        action: str,
        qty: float,
        order_type: str,
        limit_price: float | None,
        stop_price: float | None,
        oca_group: str | None,
        status: str,
        symbol: str | None = None,
    ) -> int:
        """`signal_id` may be None for an exit with no known originating
        signal (an emergency/EOD flatten on a symbol with no tracked lot) --
        pass `symbol` in that case so the row is still attributable. While
        the column was NOT NULL those exits could not be recorded at all,
        which is the main reason only ~45% of traded notional had a
        journaled exit.

        limit_price/stop_price are sanitised: ib_async's Order declares both
        lmtPrice and auxPrice defaulting to UNSET_DOUBLE (1.797e308), and the
        attribute always exists, so `getattr(order, "auxPrice", None)` never
        returns None -- it returns the sentinel. 1,038 of 2,190 order rows
        (47%) carried it before this."""
        cur = self.conn.execute(
            """INSERT INTO orders
               (signal_id, symbol, ib_order_id, role, action, qty, order_type, limit_price, stop_price, oca_group, status, ts_submitted, ts_last_update)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                signal_id,
                symbol,
                ib_order_id,
                role,
                action,
                qty,
                order_type,
                _real_price(limit_price),
                _real_price(stop_price),
                oca_group,
                status,
                _now(),
                _now(),
            ),
        )
        self.conn.commit()
        return cur.lastrowid

    def update_order_status(self, order_row_id: int, status: str) -> None:
        self.conn.execute(
            "UPDATE orders SET status = ?, ts_last_update = ? WHERE id = ?",
            (status, _now(), order_row_id),
        )
        self.conn.commit()

    def update_order_price(
        self,
        order_row_id: int,
        limit_price: float | None = None,
        stop_price: float | None = None,
        qty: float | None = None,
    ) -> None:
        """Records an in-place modification (breakeven move, trailing
        ratchet, or scale-out resize) against the order's existing row,
        rather than inserting a synthetic new order."""
        fields = ["ts_last_update = ?"]
        params: list = [_now()]
        if limit_price is not None:
            fields.append("limit_price = ?")
            params.append(limit_price)
        if stop_price is not None:
            fields.append("stop_price = ?")
            params.append(stop_price)
        if qty is not None:
            fields.append("qty = ?")
            params.append(qty)
        params.append(order_row_id)
        self.conn.execute(f"UPDATE orders SET {', '.join(fields)} WHERE id = ?", params)
        self.conn.commit()

    def record_fill(
        self,
        order_row_id: int,
        ib_order_id: int,
        fill_qty: float,
        fill_price: float,
        commission: float | None = None,
        realized_pnl: float | None = None,
        exec_id: str | None = None,
        exec_ts: str | None = None,
    ) -> int:
        """INSERT OR IGNORE on IBKR's globally-unique execution id.

        This is what makes fill dedup exact. Without exec_id a re-delivered
        fill and a genuine repeat partial at the same size and price are
        indistinguishable by construction -- which is why
        dashboard_report.dedup_stop_fills exists, and that heuristic only
        filters role='stop', missing the scale_out overfills (7 of 11 orders,
        up to 2.95x) that bias reported P&L upward on exactly the profitable
        exits.

        commission/realized_pnl are deliberately left None here: ib_async
        emits fillEvent with an EMPTY CommissionReport and only populates it
        later via a separate message, so reading it synchronously always
        yields 0.0 -- live or paper. See update_fill_commission."""
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO fills
               (order_id, ib_order_id, ts, exec_id, exec_ts, fill_qty, fill_price, commission, realized_pnl)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (order_row_id, ib_order_id, _now(), exec_id, exec_ts, fill_qty, fill_price, commission, realized_pnl),
        )
        self.conn.commit()
        if cur.rowcount == 0 and exec_id:
            logger.debug("Duplicate fill ignored (exec_id=%s)", exec_id)
        return cur.lastrowid

    @contextmanager
    def transaction(self):
        """Groups several writes into one atomic unit.

        Every method here commits individually, so a signal, its risk
        decision and its order rows were three independent commits -- a
        crash between them leaves a signal with no order, or an order with
        no fill, both of which the reports read as a legitimate "never
        filled" / "still open" outcome. There is no way to tell a crash
        artifact from a real one after the fact."""
        try:
            with self.conn:  # BEGIN ... COMMIT, ROLLBACK on exception
                yield self
        except Exception:
            logger.exception("Journal transaction rolled back")
            raise

    def update_fill_commission(
        self, exec_id: str, commission: float | None, realized_pnl: float | None
    ) -> bool:
        """Fills in the commission once IBKR actually sends it.

        ib_async's wrapper.execDetails emits fillEvent immediately with
        `CommissionReport()` at its defaults (commission=0.0,
        realizedPNL=0.0); the real values arrive in a separate
        commissionReport message, re-emitted as ib.commissionReportEvent
        keyed on execId. Every commission in this journal was 0.00 because
        the bot only ever read the former."""
        cur = self.conn.execute(
            "UPDATE fills SET commission = ?, realized_pnl = ? WHERE exec_id = ?",
            (commission, realized_pnl, exec_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def record_account_snapshot(self, snapshot: AccountSnapshot) -> None:
        self.conn.execute(
            """INSERT INTO account_snapshots (ts, net_liquidation, buying_power, daily_realized_pnl, open_positions_count)
               VALUES (?, ?, ?, ?, ?)""",
            (_now(), snapshot.net_liquidation, snapshot.buying_power, snapshot.daily_realized_pnl, snapshot.open_positions_count),
        )
        self.conn.commit()

    def record_heartbeat(
        self,
        connected: bool,
        symbols_subscribed: int,
        bars_received_last_min: int,
        signals_today: int,
        open_positions: int,
        breadth: int | None = None,
        scanner_refusals: int = 0,
        seconds_since_scan: float | None = None,
    ) -> None:
        """One row per minute of liveness -- see bot_heartbeat in db.py."""
        self.conn.execute(
            """INSERT INTO bot_heartbeat
               (ts, connected, symbols_subscribed, bars_received_last_min, signals_today,
                open_positions, breadth, scanner_refusals, seconds_since_scan)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                _now(),
                1 if connected else 0,
                symbols_subscribed,
                bars_received_last_min,
                signals_today,
                open_positions,
                breadth,
                scanner_refusals,
                seconds_since_scan,
            ),
        )
        self.conn.commit()

    def find_order_by_ib_order_id(self, ib_order_id: int) -> dict | None:
        """Looks up the journal row (+ role, entry price) for a previously
        recorded order, keyed by IBKR's own order id -- used to re-attach
        fill/status tracking to orders that are still resting at IBKR from
        before a process restart (see OrderManager.resync_open_orders)."""
        row = self.conn.execute(
            """SELECT o.id AS row_id, o.role, s.entry_price
               FROM orders o JOIN signals s ON s.id = o.signal_id
               WHERE o.ib_order_id = ?""",
            (ib_order_id,),
        ).fetchone()
        if row is None:
            return None
        return {"row_id": row[0], "role": row[1], "entry_price": row[2]}

    def record_kill_switch_event(self, triggered_by: str, action_taken: str) -> None:
        self.conn.execute(
            "INSERT INTO kill_switch_events (ts, triggered_by, action_taken) VALUES (?, ?, ?)",
            (_now(), triggered_by, action_taken),
        )
        self.conn.commit()

    def save_daily_risk_state(self, trading_date: str, start_of_day_equity: float, loss_limit_halted: bool) -> None:
        """Upserts today's risk baseline/halt state -- see db.py's
        daily_risk_state schema comment for why this exists. Called both
        the moment a fresh trading day's baseline is established and
        periodically thereafter (main.py's risk loop) so a halt that trips
        mid-day is persisted promptly, not just at start-of-day."""
        self.conn.execute(
            """INSERT INTO daily_risk_state (trading_date, start_of_day_equity, loss_limit_halted, ts_updated)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(trading_date) DO UPDATE SET
                   start_of_day_equity = excluded.start_of_day_equity,
                   loss_limit_halted = excluded.loss_limit_halted,
                   ts_updated = excluded.ts_updated""",
            (trading_date, start_of_day_equity, int(loss_limit_halted), _now()),
        )
        self.conn.commit()

    def load_daily_risk_state(self, trading_date: str) -> dict | None:
        """The persisted baseline/halt state for `trading_date` (an ET
        date's isoformat string), or None if this process has never
        established one for that date yet -- the caller's cue to compute a
        fresh baseline instead of restoring one."""
        row = self.conn.execute(
            "SELECT start_of_day_equity, loss_limit_halted FROM daily_risk_state WHERE trading_date = ?",
            (trading_date,),
        ).fetchone()
        if row is None:
            return None
        return {"start_of_day_equity": row[0], "loss_limit_halted": bool(row[1])}

    def save_symbol_loss(
        self,
        trading_date: str,
        symbol: str,
        losing_lots: int,
        last_exit_role: str | None = None,
        last_realized_pnl: float | None = None,
    ) -> None:
        """Upserts how many lots in `symbol` have closed red on
        `trading_date` -- see db.py's symbol_loss_state schema comment for
        why the symbol_loss_cap gate cannot rely on memory alone.

        Writes the ABSOLUTE count rather than `losing_lots + 1`: the
        in-memory Counter is rehydrated at startup, so it is always the
        authoritative post-increment value, and an absolute write stays
        idempotent if a fill callback ever double-fires. An incrementing
        UPDATE would inflate the count and silently over-ban a symbol."""
        self.conn.execute(
            """INSERT INTO symbol_loss_state
                   (trading_date, symbol, losing_lots, last_exit_role, last_realized_pnl, ts_updated)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(trading_date, symbol) DO UPDATE SET
                   losing_lots = excluded.losing_lots,
                   last_exit_role = excluded.last_exit_role,
                   last_realized_pnl = excluded.last_realized_pnl,
                   ts_updated = excluded.ts_updated""",
            (trading_date, symbol, int(losing_lots), last_exit_role, last_realized_pnl, _now()),
        )
        self.conn.commit()

    def load_symbol_losses(self, trading_date: str) -> dict[str, int]:
        """Per-symbol losing-lot counts for `trading_date` (an ET date's
        isoformat string), for rehydrating the symbol_loss_cap gate on
        startup.

        Returns an empty dict rather than None when the day has no rows:
        unlike daily_risk_state's equity baseline, "no losses yet" and
        "never established" are the same state here, so there is no
        caller branch to signal."""
        rows = self.conn.execute(
            "SELECT symbol, losing_lots FROM symbol_loss_state WHERE trading_date = ?",
            (trading_date,),
        ).fetchall()
        return {row[0]: row[1] for row in rows}

    def record_entry_ineligible(self, symbol: str, error_code: int, reason: str) -> None:
        """Remembers that IBKR refused to let this account open `symbol`.

        Not keyed by date -- see db.py's entry_ineligible_symbols schema
        comment. first_seen is preserved across repeats so the row shows
        when the restriction was first hit, while last_seen/rejections
        record that it is still in force."""
        self.conn.execute(
            """INSERT INTO entry_ineligible_symbols
                   (symbol, error_code, reason, first_seen, last_seen, rejections)
               VALUES (?, ?, ?, ?, ?, 1)
               ON CONFLICT(symbol) DO UPDATE SET
                   error_code = excluded.error_code,
                   reason = excluded.reason,
                   last_seen = excluded.last_seen,
                   rejections = entry_ineligible_symbols.rejections + 1""",
            (symbol, int(error_code), reason, _now(), _now()),
        )
        self.conn.commit()

    def load_entry_ineligible(self) -> dict[str, str]:
        """Every symbol this account may not open, as {symbol: reason},
        for rehydrating the entry_ineligible gate on startup."""
        rows = self.conn.execute(
            "SELECT symbol, reason FROM entry_ineligible_symbols"
        ).fetchall()
        return {row[0]: row[1] for row in rows}
