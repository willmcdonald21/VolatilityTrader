from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime, time
from typing import TYPE_CHECKING

from warrior_bot.config import RiskConfig
from warrior_bot.logging_setup import alert
from warrior_bot.risk.account_state import AccountSnapshot, AccountState
from warrior_bot.signals.signal import Signal
from warrior_bot.utils.time_utils import to_eastern

logger = logging.getLogger("warrior_bot.risk.risk_manager")

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
        no_entry_after_et: time | None = None,
    ):
        self.config = config
        self.account_state = account_state
        self.position_manager = position_manager
        self.kill_switch_path = kill_switch_path
        # Sourced from exits.eod_flatten_time by the caller (main.py) rather
        # than duplicated onto RiskConfig -- one clock governs both "stop
        # opening new positions" and "force-close whatever's open", so they
        # can't drift apart. None (the default, used by callers/tests that
        # don't pass one) disables the gate entirely, matching
        # no_entry_before_et's existing "no now supplied -> permissive"
        # behavior below.
        self.no_entry_after_et = no_entry_after_et
        self._manual_kill_switch = False
        self._start_of_day_equity: float | None = None
        # Latched, not re-derived: once the daily loss limit is breached
        # once, evaluate() below refuses every signal for the rest of the
        # trading day, however P&L moves afterward. Before this, the
        # breach check re-ran fresh on every signal -- daily_realized_pnl
        # and the open-position unrealized loss both move continuously, so
        # a partial recovery (a stop-out closing, a winner offsetting)
        # silently reopened the door to new entries on a day already
        # flagged as bad. Confirmed live, 2026-09-22: the limit was
        # breached and flattened three separate times in one session
        # (04:13, 13:58, 15:47 ET), each recovery followed by a fresh round
        # of new positions -- exactly the "stop for the day" the alert
        # message already claimed but the code never enforced.
        self._loss_limit_halted_today = False

    def activate_kill_switch(self) -> None:
        self._manual_kill_switch = True

    def deactivate_kill_switch(self) -> None:
        self._manual_kill_switch = False

    def _kill_switch_active(self) -> bool:
        return self._manual_kill_switch or self.kill_switch_path.exists()

    def mark_start_of_day(self, equity: float) -> None:
        """Establishes a FRESH baseline for a genuinely new trading day --
        always clears the halt. Never call this to resume a process
        mid-day; use load_state for that (see its docstring for why the
        distinction matters)."""
        self._start_of_day_equity = equity
        self._loss_limit_halted_today = False

    def load_state(self, start_of_day_equity: float, loss_limit_halted: bool) -> None:
        """Restores a previously-established baseline/halt as-is -- used
        when this process is restarting partway through a trading day that
        already has persisted state (see Journal.load_daily_risk_state),
        as opposed to mark_start_of_day's unconditional fresh start.
        Deliberately does NOT reset the halt: a restart mid-day must not be
        able to undo an already-tripped daily loss limit. Confirmed live,
        2026-09-23: three same-day restarts each called mark_start_of_day
        instead of this, silently re-arming a halt that had already fired
        and re-exposing the account to new entries each time."""
        self._start_of_day_equity = start_of_day_equity
        self._loss_limit_halted_today = loss_limit_halted

    @property
    def start_of_day_equity(self) -> float | None:
        return self._start_of_day_equity

    @property
    def loss_limit_halted_today(self) -> bool:
        return self._loss_limit_halted_today

    def _loss_limit_breached(self, snapshot: AccountSnapshot) -> bool:
        if self._start_of_day_equity is None:
            return False
        loss_limit = self._start_of_day_equity * self.config.daily_loss_limit_pct
        return self._daily_pnl_for_limit(snapshot) <= -loss_limit

    def _daily_pnl_for_limit(self, snapshot: AccountSnapshot) -> float:
        """Realized P&L plus the open positions' unrealized LOSS (gains that
        haven't been banked don't offset it). Realized-only let positions
        bleed unchecked past the limit on 2026-09-21."""
        pnl = snapshot.daily_realized_pnl
        if self.config.count_unrealized_loss_in_daily_limit:
            pnl += min(0.0, snapshot.daily_unrealized_pnl)
        return pnl

    def should_flatten_for_loss_limit(self, snapshot: AccountSnapshot) -> bool:
        return self.config.flatten_on_daily_loss_limit and self._loss_limit_breached(snapshot)

    def _open_position_count(self, snapshot: AccountSnapshot) -> int:
        """Symbols IBKR shows as held UNION symbols with a bracket already
        submitted. The broker's count alone lags every fill, so signals
        evaluated in the same second all saw zero open positions and 7
        symbols were bought against a cap of 5 on 2026-09-21."""
        pending = self.position_manager.tracked_symbols()
        return max(snapshot.open_positions_count, len(snapshot.open_symbols | pending))

    def evaluate(self, signal: Signal, now: datetime | None = None) -> RiskDecision:
        snapshot = self.account_state.snapshot()

        if self._start_of_day_equity is None:
            self._start_of_day_equity = snapshot.net_liquidation

        if self._kill_switch_active():
            reason = "kill switch active"
            alert(f"Signal for {signal.symbol} ({signal.strategy}) rejected: {reason}", channel="kill_switch")
            return RiskDecision(False, 0, reason, snapshot)

        if self._loss_limit_halted_today or self._loss_limit_breached(snapshot):
            loss_limit = self._start_of_day_equity * self.config.daily_loss_limit_pct
            if not self._loss_limit_halted_today:
                self._loss_limit_halted_today = True
                logger.warning(
                    "Daily loss limit breached -- halting new entries for the rest of the trading day "
                    "(realized+unrealized %.2f <= -%.2f)",
                    self._daily_pnl_for_limit(snapshot),
                    loss_limit,
                )
            reason = (
                f"daily loss limit breached: realized {self._daily_pnl_for_limit(snapshot):.2f} "
                f"<= -{loss_limit:.2f} -- halted for the rest of the trading day"
            )
            alert(f"Signal for {signal.symbol} ({signal.strategy}) rejected: {reason}", channel="limits")
            return RiskDecision(False, 0, reason, snapshot)

        # Only applied when the caller supplies a clock reading -- never
        # guessed from wall-clock time, so evaluate() stays deterministic.
        if now is not None and to_eastern(now).time() < self.config.no_entry_before_et:
            reason = f"entry window not open until {self.config.no_entry_before_et.strftime('%H:%M')} ET"
            alert(f"Signal for {signal.symbol} ({signal.strategy}) rejected: {reason}")  # routine, log only
            return RiskDecision(False, 0, reason, snapshot)

        # A position opened after the EOD flatten cutoff has nothing left
        # to close it that day: _trigger_flatten's 15:55 sweep only fires
        # once and has already run (or is about to, this very tick) by the
        # time an entry this late could fill. Confirmed live, 2026-09-21:
        # AUUD signalled at 16:49 ET -- an hour past the 15:55 cutoff --
        # filled, and sat open through the midnight daily reset and into
        # the next morning's premarket before anything flattened it.
        if (
            now is not None
            and self.no_entry_after_et is not None
            and to_eastern(now).time() >= self.no_entry_after_et
        ):
            reason = f"entry window closed at {self.no_entry_after_et.strftime('%H:%M')} ET (EOD flatten cutoff)"
            alert(f"Signal for {signal.symbol} ({signal.strategy}) rejected: {reason}")  # routine, log only
            return RiskDecision(False, 0, reason, snapshot)

        open_count = self._open_position_count(snapshot)
        if open_count >= self.config.max_concurrent_positions:
            reason = f"max concurrent positions reached ({open_count})"
            alert(f"Signal for {signal.symbol} ({signal.strategy}) rejected: {reason}")  # routine, log only
            return RiskDecision(False, 0, reason, snapshot)

        unrestricted_capacity = self.config.max_concurrent_positions - self.config.reserved_top_tier_slots
        if open_count >= unrestricted_capacity:
            # Every unrestricted slot is taken -- only the reserved top-tier
            # slot(s) remain. No separate bookkeeping of which symbols used
            # which slot is needed: open_positions_count alone tells us
            # whether we're in reserved territory, since evaluate() is the
            # sole gate an order passes through before this count can grow.
            # A missing rank means the symbol is not in the scanner's
            # current top-N at all (main.py clears the rank of any tracked
            # symbol that drops out of a scan) -- ineligible for a slot
            # reserved for the day's most obvious names, same as a symbol
            # ranked below the cutoff.
            scanner_rank = signal.context.get("scanner_rank")
            if scanner_rank is None or scanner_rank > self.config.reserved_top_tier_max_rank:
                reason = (
                    f"remaining slot reserved for scanner_rank <= {self.config.reserved_top_tier_max_rank} "
                    f"(open positions: {open_count})"
                )
                alert(f"Signal for {signal.symbol} ({signal.strategy}) rejected: {reason}")  # routine, log only
                return RiskDecision(False, 0, reason, snapshot)

        open_lots = self.position_manager.open_lot_count(signal.symbol)
        if open_lots >= 2:
            reason = f"already at max lots (2) for {signal.symbol}"
            alert(f"Signal for {signal.symbol} ({signal.strategy}) rejected: {reason}")  # routine, log only
            return RiskDecision(False, 0, reason, snapshot)

        if open_lots >= 1:
            # A symbol already held by a strategy other than this signal's
            # own is not a pyramid add-on -- it's a second, uncoordinated
            # strategy piling onto a position it had no part in opening,
            # each with its own entry/stop/target and neither aware of the
            # other. Confirmed in the journal: this pattern wins 10% of the
            # time overall; 2026-09-22 alone had 7 instances (-$578.62,
            # over half that day's loss). Every existing lot being the SAME
            # strategy as this signal (the normal pyramid case) leaves this
            # set empty and falls through to the age check below, unchanged.
            holder_strategies = self.position_manager.open_lot_strategies(signal.symbol)
            other_strategies = holder_strategies - {signal.strategy}
            if other_strategies and not self.config.allow_cross_strategy_stacking:
                reason = (
                    f"cross_strategy_lot_conflict: {signal.symbol} already held by "
                    f"{', '.join(sorted(other_strategies))}"
                )
                alert(f"Signal for {signal.symbol} ({signal.strategy}) rejected: {reason}")  # routine, log only
                return RiskDecision(False, 0, reason, snapshot)

            age = self.position_manager.seconds_since_first_entry(signal.symbol)
            if age is not None and age < self.config.addon_min_seconds_after_first_entry:
                reason = (
                    f"add-on for {signal.symbol} too soon after first entry "
                    f"({age:.0f}s < {self.config.addon_min_seconds_after_first_entry:.0f}s)"
                )
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
        notional_caps = [raw_shares, cap_by_pct_of_buying_power]

        caps = list(notional_caps)
        shares_by_risk = self._shares_by_risk_budget(signal, snapshot, open_lots)
        if shares_by_risk is not None:
            caps.append(shares_by_risk)

        sized_qty = max(0, min(caps))

        multiplier = self._quality_size_multiplier(signal)
        if multiplier > 1.0 and sized_qty > 0:
            boosted = math.floor(sized_qty * multiplier)
            sized_qty = min(boosted, *notional_caps)

        if self.config.daily_profit_goal_usd and not self._cushion_met(snapshot):
            sized_qty = math.floor(sized_qty * self.config.cushion_size_fraction)

        return sized_qty

    def _quality_size_multiplier(self, signal: Signal) -> float:
        """Soft size boost for entry-quality signals computed at signal time
        (round_number_breakout, flat_top_breakout) but never previously
        acted on anywhere downstream -- see RiskConfig's fields of the same
        name. Boosts the risk-based figure specifically; the notional/
        buying-power ceilings in _size_position stay hard caps regardless of
        quality."""
        multiplier = 1.0
        if signal.context.get("round_number_breakout"):
            multiplier *= self.config.round_number_size_multiplier
        if signal.context.get("flat_top_breakout"):
            multiplier *= self.config.flat_top_size_multiplier
        return multiplier

    def _shares_by_risk_budget(
        self, signal: Signal, snapshot: AccountSnapshot, open_lots: int
    ) -> int | None:
        """Shares whose worst case (a fill at the stop) costs exactly the
        configured risk budget. None when risk-based sizing is off.

        Equity is taken from start-of-day rather than the live snapshot so
        the budget is a fixed dollar amount for the whole session, instead
        of shrinking with each loss and compounding a drawdown into
        progressively smaller size."""
        risk_pct = (
            self.config.risk_per_trade_pct
            if open_lots == 0
            else (self.config.addon_risk_pct or self.config.risk_per_trade_pct)
        )
        if risk_pct is None:
            return None
        # Quantized to sub-penny tick precision before dividing: entry and
        # stop are each tick-rounded, but their difference still carries
        # float residue (2.00 - 1.90 = 0.10000000000000009), which flooring
        # turns into a silently missing share on otherwise exact numbers.
        risk_per_share = round(signal.risk_per_share, 4)
        if risk_per_share <= 0:
            return None
        equity = self._start_of_day_equity or snapshot.net_liquidation
        return math.floor((equity * risk_pct) / risk_per_share)

    def _cushion_met(self, snapshot: AccountSnapshot) -> bool:
        """Warrior Trading's 'profit cushion' rule: trade at reduced size
        until a fraction of the daily profit goal is already banked, then
        size back up to full. Re-evaluated on every signal (not a one-way
        ratchet), so size drops back down again if the cushion erodes."""
        cushion_target = self.config.daily_profit_goal_usd * self.config.cushion_profit_fraction
        return snapshot.daily_realized_pnl >= cushion_target
