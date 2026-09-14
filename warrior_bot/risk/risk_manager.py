from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from warrior_bot.config import RiskConfig
from warrior_bot.logging_setup import alert
from warrior_bot.risk.account_state import AccountSnapshot, AccountState
from warrior_bot.signals.signal import Signal

if TYPE_CHECKING:
    # Deferred to a type-checking-only import: warrior_bot.persistence.journal
    # imports RiskDecision from this module, and PositionManager imports
    # Journal -- an unconditional import here would create an import cycle.
    # Safe because `from __future__ import annotations` (above) makes every
    # annotation in this file a lazily-evaluated string.
    from warrior_bot.execution.position_manager import PositionManager


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

    def __init__(
        self,
        config: RiskConfig,
        account_state: AccountState,
        position_manager: PositionManager,
        kill_switch_path: Path,
    ):
        self.config = config
        self.account_state = account_state
        self.position_manager = position_manager
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

    @property
    def start_of_day_equity(self) -> float | None:
        return self._start_of_day_equity

    def _loss_limit_breached(self, snapshot: AccountSnapshot) -> bool:
        if self._start_of_day_equity is None:
            return False
        loss_limit = self._start_of_day_equity * self.config.daily_loss_limit_pct
        return snapshot.daily_realized_pnl <= -loss_limit

    def should_flatten_for_loss_limit(self, snapshot: AccountSnapshot) -> bool:
        return self.config.flatten_on_daily_loss_limit and self._loss_limit_breached(snapshot)

    def evaluate(self, signal: Signal) -> RiskDecision:
        snapshot = self.account_state.snapshot()

        if self._start_of_day_equity is None:
            self._start_of_day_equity = snapshot.net_liquidation

        if self._kill_switch_active():
            reason = "kill switch active"
            alert(f"Signal for {signal.symbol} ({signal.strategy}) rejected: {reason}", channel="kill_switch")
            return RiskDecision(False, 0, reason, snapshot)

        if self._loss_limit_breached(snapshot):
            loss_limit = self._start_of_day_equity * self.config.daily_loss_limit_pct
            reason = (
                f"daily loss limit breached: realized {snapshot.daily_realized_pnl:.2f} "
                f"<= -{loss_limit:.2f}"
            )
            alert(f"Signal for {signal.symbol} ({signal.strategy}) rejected: {reason}", channel="limits")
            return RiskDecision(False, 0, reason, snapshot)

        if snapshot.open_positions_count >= self.config.max_concurrent_positions:
            reason = f"max concurrent positions reached ({snapshot.open_positions_count})"
            alert(f"Signal for {signal.symbol} ({signal.strategy}) rejected: {reason}")  # routine, log only
            return RiskDecision(False, 0, reason, snapshot)

        unrestricted_capacity = self.config.max_concurrent_positions - self.config.reserved_top_tier_slots
        if snapshot.open_positions_count >= unrestricted_capacity:
            # Every unrestricted slot is taken -- only the reserved top-tier
            # slot(s) remain. No separate bookkeeping of which symbols used
            # which slot is needed: open_positions_count alone tells us
            # whether we're in reserved territory, since evaluate() is the
            # sole gate an order passes through before this count can grow.
            scanner_rank = signal.context.get("scanner_rank")
            if scanner_rank is None:
                # Every onboarded symbol is expected to carry a scanner_rank
                # -- this shouldn't happen. Treat it as ineligible for the
                # reserved slot rather than crashing or silently admitting it.
                alert(
                    f"Signal for {signal.symbol} ({signal.strategy}) has no scanner_rank while the "
                    "reserved top-tier slot logic is evaluating it -- every onboarded symbol should carry one"
                )
            if scanner_rank is None or scanner_rank > self.config.reserved_top_tier_max_rank:
                reason = (
                    f"remaining slot reserved for scanner_rank <= {self.config.reserved_top_tier_max_rank} "
                    f"(open positions: {snapshot.open_positions_count})"
                )
                alert(f"Signal for {signal.symbol} ({signal.strategy}) rejected: {reason}")  # routine, log only
                return RiskDecision(False, 0, reason, snapshot)

        open_lots = self.position_manager.open_lot_count(signal.symbol)
        if open_lots >= 2:
            reason = f"already at max lots (2) for {signal.symbol}"
            alert(f"Signal for {signal.symbol} ({signal.strategy}) rejected: {reason}")  # routine, log only
            return RiskDecision(False, 0, reason, snapshot)

        sized_qty = self._size_position(signal, snapshot, open_lots)
        if sized_qty < 1:
            reason = "position size rounds to zero under current risk caps"
            alert(f"Signal for {signal.symbol} ({signal.strategy}) rejected: {reason}")  # routine, log only
            return RiskDecision(False, 0, reason, snapshot)

        return RiskDecision(True, sized_qty, "accepted", snapshot)

    def _size_position(self, signal: Signal, snapshot: AccountSnapshot, open_lots: int) -> int:
        if signal.entry_price <= 0:
            return 0

        # First entry into a symbol sizes off first_entry_pct_of_funds; a
        # second signal on a symbol already holding one lot sizes off the
        # smaller addon_pct_of_funds (the pyramid add-on). evaluate()
        # already rejects a third signal (open_lots >= 2) before this is
        # ever called.
        pct = self.config.first_entry_pct_of_funds if open_lots == 0 else self.config.addon_pct_of_funds
        raw_shares = math.floor(snapshot.available_funds * pct / signal.entry_price)

        # Outer safety backstop, relative to current buying power -- the
        # %-of-funds numbers above are expected to sit comfortably under
        # this, but it stays as a hard ceiling regardless.
        cap_by_pct_of_buying_power = math.floor(
            (snapshot.buying_power * self.config.max_position_pct_of_buying_power) / signal.entry_price
        )

        sized_qty = max(0, min(raw_shares, cap_by_pct_of_buying_power))

        if self.config.daily_profit_goal_usd and not self._cushion_met(snapshot):
            sized_qty = math.floor(sized_qty * self.config.cushion_size_fraction)

        return sized_qty

    def _cushion_met(self, snapshot: AccountSnapshot) -> bool:
        """Warrior Trading's 'profit cushion' rule: trade at reduced size
        until a fraction of the daily profit goal is already banked, then
        size back up to full. Re-evaluated on every signal (not a one-way
        ratchet), so size drops back down again if the cushion erodes."""
        cushion_target = self.config.daily_profit_goal_usd * self.config.cushion_profit_fraction
        return snapshot.daily_realized_pnl >= cushion_target
