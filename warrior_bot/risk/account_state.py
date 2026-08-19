from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from ib_async import IB
from ib_async.util import UNSET_DOUBLE


@dataclass
class AccountSnapshot:
    net_liquidation: float
    available_funds: float
    buying_power: float
    open_positions_count: int
    daily_realized_pnl: float


class AccountState:
    """Polls IBKR for ground truth rather than trusting local counters.

    Orders can be modified/cancelled directly in TWS and fills can be
    partial, so open-position counts and realized PnL are re-derived from
    IBKR's own reported state on every risk check, not accumulated locally.
    """

    def __init__(self, ib: IB, account: str = ""):
        self.ib = ib
        self.account = account
        self._session_start = datetime.now(timezone.utc)

    def reset_session(self) -> None:
        self._session_start = datetime.now(timezone.utc)

    def _account_value(self, tag: str) -> float:
        for av in self.ib.accountValues(self.account):
            if av.tag == tag and (not self.account or av.account == self.account):
                try:
                    return float(av.value)
                except ValueError:
                    return 0.0
        return 0.0

    def _closing_fills(self) -> list[tuple[datetime, float]]:
        """(fill_time, realized_pnl) for today's closing fills only.

        IB's CommissionReport.realizedPNL carries a sentinel value
        (UNSET_DOUBLE, i.e. sys.float_info.max) for the opening leg of a
        round trip rather than 0 — those must be excluded or they blow up
        the total, not just skipped via a falsy check.
        """
        closing: list[tuple[datetime, float]] = []
        for fill in self.ib.fills():
            fill_time = fill.time
            if fill_time.tzinfo is None:
                fill_time = fill_time.replace(tzinfo=timezone.utc)
            if fill_time < self._session_start:
                continue
            pnl = fill.commissionReport.realizedPNL
            if pnl is None or abs(pnl) >= UNSET_DOUBLE / 2:
                continue
            closing.append((fill_time, pnl))
        return closing

    def daily_realized_pnl(self) -> float:
        return sum(pnl for _, pnl in self._closing_fills())

    def first_closing_trade_pnl(self) -> float | None:
        """Realized P&L of the session's earliest closing fill, or None if
        nothing has closed yet -- the "did today's starter trade work"
        regime signal (source material: take reduced size on the day's
        first trade; if it fails, treat that as a cold-market caution flag
        and scale back for the rest of the session).

        An approximation of "the first trade's outcome," not exact trade
        grouping: a trade closed via a scale-out followed by a later
        stop-out produces two closing fills, and this reports only the
        earlier one's sign. That matches the source material's own binary
        framing (did the starter trade work or not) closely enough without
        needing full round-trip trade-grouping logic.
        """
        closing = sorted(self._closing_fills(), key=lambda item: item[0])
        return closing[0][1] if closing else None

    def snapshot(self) -> AccountSnapshot:
        return AccountSnapshot(
            net_liquidation=self._account_value("NetLiquidation"),
            available_funds=self._account_value("AvailableFunds"),
            buying_power=self._account_value("BuyingPower"),
            open_positions_count=len([p for p in self.ib.positions(self.account) if p.position != 0]),
            daily_realized_pnl=self.daily_realized_pnl(),
        )
