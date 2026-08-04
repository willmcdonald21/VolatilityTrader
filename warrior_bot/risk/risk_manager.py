from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from warrior_bot.config import RiskConfig
from warrior_bot.logging_setup import alert
from warrior_bot.risk.account_state import AccountSnapshot, AccountState
from warrior_bot.signals.signal import Signal


@dataclass
class RiskDecision:
    accepted: bool
    sized_qty: int
    reason: str
    snapshot: AccountSnapshot


class RiskManager:
    """Every signal must pass through here before an order reaches IBKR.

    Rules are checked in a fixed order (kill switch -> daily loss halt ->
    max concurrent positions -> position sizing) so the rejection reason is
    always the first blocking condition, not the last one evaluated.
    """

    def __init__(self, config: RiskConfig, account_state: AccountState, kill_switch_path: Path):
        self.config = config
        self.account_state = account_state
        self.kill_switch_path = kill_switch_path
        self._manual_kill_switch = False
        self._start_of_day_equity: float | None = None

    def activate_kill_switch(self) -> None:
        self._manual_kill_switch = True

    def deactivate_kill_switch(self) -> None:
        self._manual_kill_switch = False

    def _kill_switch_active(self) -> bool:
        return self._manual_kill_switch or self.kill_switch_path.exists()

    def mark_start_of_day(self, equity: float) -> None:
        self._start_of_day_equity = equity

    def evaluate(self, signal: Signal) -> RiskDecision:
        snapshot = self.account_state.snapshot()

        if self._start_of_day_equity is None:
            self._start_of_day_equity = snapshot.net_liquidation

        if self._kill_switch_active():
            reason = "kill switch active"
            alert(f"Signal for {signal.symbol} ({signal.strategy}) rejected: {reason}")
            return RiskDecision(False, 0, reason, snapshot)

        loss_limit = self._start_of_day_equity * self.config.daily_loss_limit_pct
        if snapshot.daily_realized_pnl <= -loss_limit:
            reason = (
                f"daily loss limit breached: realized {snapshot.daily_realized_pnl:.2f} "
                f"<= -{loss_limit:.2f}"
            )
            alert(f"Signal for {signal.symbol} ({signal.strategy}) rejected: {reason}")
            return RiskDecision(False, 0, reason, snapshot)

        if snapshot.open_positions_count >= self.config.max_concurrent_positions:
            reason = f"max concurrent positions reached ({snapshot.open_positions_count})"
            alert(f"Signal for {signal.symbol} ({signal.strategy}) rejected: {reason}")
            return RiskDecision(False, 0, reason, snapshot)

        sized_qty = self._size_position(signal, snapshot)
        if sized_qty < 1:
            reason = "position size rounds to zero under current risk caps"
            alert(f"Signal for {signal.symbol} ({signal.strategy}) rejected: {reason}")
            return RiskDecision(False, 0, reason, snapshot)

        return RiskDecision(True, sized_qty, "accepted", snapshot)

    def _size_position(self, signal: Signal, snapshot: AccountSnapshot) -> int:
        risk_per_share = signal.risk_per_share
        if risk_per_share <= 0 or signal.entry_price <= 0:
            return 0

        dollar_risk_budget = snapshot.net_liquidation * self.config.risk_per_trade_pct
        raw_shares = math.floor(dollar_risk_budget / risk_per_share)

        cap_by_notional = math.floor(self.config.max_position_notional_usd / signal.entry_price)
        cap_by_buying_power = math.floor(snapshot.buying_power / signal.entry_price)
        cap_by_abs_shares = self.config.max_shares_per_trade

        return max(0, min(raw_shares, cap_by_notional, cap_by_buying_power, cap_by_abs_shares))
