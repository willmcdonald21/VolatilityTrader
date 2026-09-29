from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from ib_async import IB
from ib_async.util import UNSET_DOUBLE

from warrior_bot.utils.time_utils import session_date_start


@dataclass
class AccountSnapshot:
    # None means IBKR has not reported the figure (typically a fresh
    # reconnect before account updates have resynced) -- callers must
    # distinguish that from a genuine zero. See AccountState._account_value
    # for the live incident this guards against.
    net_liquidation: float | None
    available_funds: float | None
    buying_power: float | None
    open_positions_count: int
    daily_realized_pnl: float
    # Symbols IBKR reports a nonzero position in, and the summed unrealized
    # P&L of those positions. Defaulted so callers that only care about the
    # original five fields keep working.
    open_symbols: frozenset = frozenset()
    daily_unrealized_pnl: float = 0.0


def _today_start_utc() -> datetime:
    """Midnight ET today, in UTC -- the boundary _todays_fills() uses to
    decide which of IBKR's cached fills count toward daily_realized_pnl.

    Deliberately NOT datetime.now(timezone.utc): that was the bug.
    AccountState is reconstructed fresh on every WarriorBot.__init__, i.e.
    every process restart (routine ones too -- the supervisor loop
    restarts on any crash). Using "now" as the fill-lookback boundary
    means a mid-day restart silently drops every fill from before the
    restart out of daily_realized_pnl, which RiskManager's daily-loss-halt
    check reads directly -- confirmed live, 2026-09-23: a restart minutes
    after the daily loss limit halted the bot for the day would have reset
    the realized P&L calculation to ~$0 and lifted the halt, letting it
    resume trading past the point it was supposed to stop. Anchoring to
    the actual start of the ET calendar day instead means daily_realized_pnl
    -- and therefore the halt -- self-heals correctly on the very next
    risk check after any restart, whatever caused it."""
    return session_date_start().astimezone(timezone.utc)


class AccountState:
    """Polls IBKR for ground truth rather than trusting local counters.

    Orders can be modified/cancelled directly in TWS and fills can be
    partial, so open-position counts and realized PnL are re-derived from
    IBKR's own reported state on every risk check, not accumulated locally.
    """

    def __init__(self, ib: IB, account: str = ""):
        self.ib = ib
        self.account = account
        self._session_start = _today_start_utc()

    def reset_session(self) -> None:
        self._session_start = _today_start_utc()

    def _account_value(self, tag: str) -> float | None:
        """None when IBKR has not reported this tag, which is NOT the same
        as zero.

        ib_async's wrapper.reset() clears accountValues on every disconnect,
        and connectAsync's account-updates sync can time out silently, so an
        empty table is a routine post-reconnect state. Returning 0.0 for it
        was a live landmine: RiskManager.evaluate seeds start_of_day_equity
        from net_liquidation when it is None, so a 0.0 reading set the
        day's baseline to zero, which made the daily loss limit
        (equity * pct) zero, which made the first cent of red P&L trip
        `<= -0.0`. That latches _loss_limit_halted_today, persists it to
        daily_risk_state, and load_state deliberately refuses to clear a
        halt on restart -- so a routine disconnect could silently end
        trading for the whole day.

        Also filters on currency: with account="" (the default),
        ib.accountValues("") returns every account and every currency, and
        IBKR publishes NetLiquidation in both USD and BASE. Taking
        whichever arrived first is benign on a USD-only paper account and
        wrong the moment that stops being true."""
        for av in self.ib.accountValues(self.account):
            if av.tag != tag or (self.account and av.account != self.account):
                continue
            if getattr(av, "currency", "") not in ("", "USD", "BASE"):
                continue
            try:
                return float(av.value)
            except (TypeError, ValueError):
                return None
        return None

    def _todays_fills(self) -> list:
        """This session's fills, oldest first — average-cost accounting
        below depends on processing them in execution order."""
        todays = []
        for fill in self.ib.fills():
            fill_time = fill.time
            if fill_time.tzinfo is None:
                fill_time = fill_time.replace(tzinfo=timezone.utc)
            if fill_time < self._session_start:
                continue
            todays.append((fill_time, fill))
        todays.sort(key=lambda pair: pair[0])
        return [fill for _, fill in todays]

    def daily_realized_pnl(self) -> float:
        """Round-trip realized P&L across this session's fills, net of
        commissions, via per-symbol average-cost matching.

        Deliberately does NOT use CommissionReport.realizedPNL. That field
        is documented to carry an UNSET_DOUBLE sentinel on the opening leg
        of a round trip, but IBKR's paper simulator also leaves it at ~0 on
        genuine *closing* fills -- every realized_pnl ever written to
        data/journal.sqlite3 is 0.0, across 2,500+ fills. Summing it
        therefore pinned this to 0.0 permanently, which silently disarmed
        the only automatic circuit breaker in the bot: RiskManager's daily
        loss limit compares against this number, so no loss -- of any size
        -- could ever trip the halt or the flatten. Recomputing from raw
        fill prices is the same method scripts/dashboard_report.py uses,
        hand-validated against a full day of fills on 2026-09-14.
        """
        qty_held: dict[str, float] = {}
        avg_cost: dict[str, float] = {}
        realized = 0.0

        for fill in self._todays_fills():
            symbol = fill.contract.symbol
            shares = float(fill.execution.shares)
            price = float(fill.execution.price)
            if fill.commissionReport is not None:
                commission = fill.commissionReport.commission
                if commission is not None and abs(commission) < UNSET_DOUBLE / 2:
                    realized -= commission

            held = qty_held.get(symbol, 0.0)
            cost = avg_cost.get(symbol, 0.0)
            if fill.execution.side == "BOT":
                total = held + shares
                avg_cost[symbol] = ((cost * held) + (price * shares)) / total if total else 0.0
                qty_held[symbol] = total
            else:
                # Only shares actually accounted for as held contribute a
                # round trip. Selling more than this session has bought
                # should never happen for a long-only bot, but it did once
                # (the 2026-09-16 NRXS naked short) -- count the matched
                # portion and leave the rest out rather than inventing a
                # cost basis for shares whose entry isn't in this session.
                matched = min(shares, held)
                realized += (price - cost) * matched
                qty_held[symbol] = held - matched

        return realized

    def unrealized_pnl(self) -> float:
        total = 0.0
        for item in self.ib.portfolio(self.account):
            if item.position == 0:
                continue
            pnl = item.unrealizedPNL
            if pnl is not None and pnl == pnl and abs(pnl) < UNSET_DOUBLE / 2:  # pnl == pnl: not NaN
                total += pnl
        return total

    def snapshot(self) -> AccountSnapshot:
        return AccountSnapshot(
            net_liquidation=self._account_value("NetLiquidation"),
            available_funds=self._account_value("AvailableFunds"),
            buying_power=self._account_value("BuyingPower"),
            open_positions_count=len([p for p in self.ib.positions(self.account) if p.position != 0]),
            daily_realized_pnl=self.daily_realized_pnl(),
            open_symbols=frozenset(p.contract.symbol for p in self.ib.positions(self.account) if p.position != 0),
            daily_unrealized_pnl=self.unrealized_pnl(),
        )
