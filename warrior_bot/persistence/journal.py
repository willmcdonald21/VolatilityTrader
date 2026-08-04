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

    def record_kill_switch_event(self, triggered_by: str, action_taken: str) -> None:
        self.conn.execute(
            "INSERT INTO kill_switch_events (ts, triggered_by, action_taken) VALUES (?, ?, ?)",
            (_now(), triggered_by, action_taken),
        )
        self.conn.commit()
