from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from warrior_bot.risk.account_state import AccountSnapshot
from warrior_bot.risk.risk_manager import RiskDecision
from warrior_bot.signals.signal import Signal


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
        signal_id: int,
        ib_order_id: int,
        role: str,
        action: str,
        qty: float,
        order_type: str,
        limit_price: float | None,
        stop_price: float | None,
        oca_group: str | None,
        status: str,
    ) -> int:
        cur = self.conn.execute(
            """INSERT INTO orders
               (signal_id, ib_order_id, role, action, qty, order_type, limit_price, stop_price, oca_group, status, ts_submitted, ts_last_update)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (signal_id, ib_order_id, role, action, qty, order_type, limit_price, stop_price, oca_group, status, _now(), _now()),
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
        commission: float | None,
        realized_pnl: float | None,
    ) -> int:
        cur = self.conn.execute(
            """INSERT INTO fills (order_id, ib_order_id, ts, fill_qty, fill_price, commission, realized_pnl)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (order_row_id, ib_order_id, _now(), fill_qty, fill_price, commission, realized_pnl),
        )
        self.conn.commit()
        return cur.lastrowid

    def record_account_snapshot(self, snapshot: AccountSnapshot) -> None:
        self.conn.execute(
            """INSERT INTO account_snapshots (ts, net_liquidation, buying_power, daily_realized_pnl, open_positions_count)
               VALUES (?, ?, ?, ?, ?)""",
            (_now(), snapshot.net_liquidation, snapshot.buying_power, snapshot.daily_realized_pnl, snapshot.open_positions_count),
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
