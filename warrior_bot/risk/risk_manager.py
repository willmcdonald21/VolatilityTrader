from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from warrior_bot.config import RiskConfig
from warrior_bot.logging_setup import alert
from warrior_bot.risk.account_state import AccountSnapshot, AccountState
from warrior_bot.signals.signal import Signal
from warrior_bot.utils.time_utils import to_eastern


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
        self._trades_accepted_today = 0

    def activate_kill_switch(self) -> None:
        self._manual_kill_switch = True

    def deactivate_kill_switch(self) -> None:
        self._manual_kill_switch = False

    def _kill_switch_active(self) -> bool:
        return self._manual_kill_switch or self.kill_switch_path.exists()

    def mark_start_of_day(self, equity: float) -> None:
        self._start_of_day_equity = equity
        self._trades_accepted_today = 0

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

    def evaluate(self, signal: Signal, now: datetime | None = None) -> RiskDecision:
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

        sized_qty = self._size_position(signal, snapshot, now)
        if sized_qty < 1:
            reason = "position size rounds to zero under current risk caps"
            alert(f"Signal for {signal.symbol} ({signal.strategy}) rejected: {reason}")  # routine, log only
            return RiskDecision(False, 0, reason, snapshot)

        self._trades_accepted_today += 1
        return RiskDecision(True, sized_qty, "accepted", snapshot)

    def _size_position(self, signal: Signal, snapshot: AccountSnapshot, now: datetime | None = None) -> int:
        risk_per_share = signal.risk_per_share
        if risk_per_share <= 0 or signal.entry_price <= 0:
            return 0

        dollar_risk_budget = snapshot.net_liquidation * self.config.risk_per_trade_pct
        raw_shares = math.floor(dollar_risk_budget / risk_per_share)

        if self._trades_accepted_today == 0:
            # Daily "starter position" test: the day's first trade is taken
            # deliberately smaller, regardless of setup quality, as a live
            # read on today's market regime -- not a confidence judgment
            # about this specific signal.
            raw_shares = math.floor(raw_shares * self.config.starter_trade_size_multiplier)
        else:
            starter_pnl = self.account_state.first_closing_trade_pnl()
            if starter_pnl is not None and starter_pnl <= 0:
                # The starter trade lost -- source material's "caution
                # flag": a valid, five-pillars-passing setup failing on the
                # very first attempt of the day is read as a cold-market
                # signal, not evidence the pattern itself is broken. Cap
                # size for every trade for the rest of the session rather
                # than relying on the human tendency to do the opposite
                # (increase size trying to make the loss back quickly).
                raw_shares = math.floor(raw_shares * self.config.starter_trade_downgrade_multiplier)

        if signal.context.get("catalyst_category"):
            # Boost applied to the raw, uncapped share count -- so it can
            # use more of the room within the hard caps below, but can never
            # push sizing past them. A catalyst earns more size within the
            # existing risk budget, not a bigger risk budget.
            raw_shares = math.floor(raw_shares * self.config.catalyst_size_multiplier)

        scanner_rank = signal.context.get("scanner_rank")
        if scanner_rank is not None and scanner_rank <= self.config.obvious_rank_threshold:
            # "Obviousness" boost -- Warrior Trading's "Dip or Dump" dump
            # checklist: a stock outside the top-N leading % gainers lacks
            # the crowd participation needed to keep absorbing profit-taking
            # during a pullback. Same soft size-boost treatment as the
            # catalyst/time-of-day multipliers, not a hard entry gate.
            raw_shares = math.floor(raw_shares * self.config.obvious_size_multiplier)

        pullback_pct = signal.context.get("pullback_pct")
        if pullback_pct is not None and pullback_pct <= self.config.shallow_pullback_threshold_pct:
            # Graduated retracement confidence -- "I'd rather see it
            # hovering in the top 25% of the move": a shallow bull_flag
            # pullback is higher-confidence than one merely under the hard
            # 50% invalidation ceiling (bull_flag.max_pullback_pct, already
            # enforced before a signal ever reaches here). Same soft
            # size-boost treatment as the other multipliers above.
            raw_shares = math.floor(raw_shares * self.config.shallow_pullback_size_multiplier)

        if now is not None:
            # Only applied when the caller supplies a clock reading -- never
            # guessed from wall-clock time, so sizing stays deterministic
            # for anyone calling evaluate()/_size_position() without a `now`.
            now_et = to_eastern(now)
            if self.config.time_of_day_boost_start <= now_et.time() < self.config.time_of_day_boost_end:
                raw_shares = math.floor(raw_shares * self.config.time_of_day_size_multiplier)

        cap_by_notional = math.floor(self.config.max_position_notional_usd / signal.entry_price)
        cap_by_buying_power = math.floor(snapshot.buying_power / signal.entry_price)
        cap_by_abs_shares = self.config.max_shares_per_trade

        sized_qty = max(0, min(raw_shares, cap_by_notional, cap_by_buying_power, cap_by_abs_shares))

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
