# Strategy enhancement decisions

Source material: Warrior Trading transcripts in the maintainer's local
notes folder (`warrior_trading_strategy_notes.md`, `warrior_trading_roadmap_notes.md`,
`warrior_trading_full_course_notes.md`, `warrior_trading_candlestick_pattern_notes.md`,
`warrior_trading_execution_risk_notes.md`). This file tracks findings from
that material that were considered but **not** implemented, and why, so
the reasoning isn't lost. Implemented findings are just... implemented;
see `bull_flag.py`/`abcd_pattern.py` (pullback validity gates),
`pullback_validity.py`, `indicators.py` (MACD, candlestick shape
detectors, first-lower-low), `base_strategy.py` (`_check_engaged` --
stock-level 9/20 MACD disengagement gate), `position_manager.py`
(reversal exit, stop-limit price tracking), `bracket_builder.py`
(stop-limit order type), `risk_manager.py` (catalyst and time-of-day size
boosts), and `config.yaml`.

## Deferred: daily "give-back" circuit breaker

Source material's daily walk-away rules include three candidate circuit
breakers:

1. **50% give-back from today's peak realized profit** — if P&L drops to
   half of its intraday high, stop trading for the day.
2. **Trailing-average daily max loss** — cap daily loss at your own
   trailing N-day average daily gain, rather than a fixed % of equity.
3. **Time-of-day performance cutoff** — stop trading once your own
   historical win-rate-by-hour shows the edge has predictably faded for
   the day.

**Status: not implemented, explicitly deferred at the maintainer's request.**
The existing `risk.daily_loss_limit_pct` (fixed % of start-of-day equity)
remains the only daily circuit breaker.

Why deferred: (2) and (3) both require a real trading history to compute
from (rolling daily P&L, win-rate by hour) — the bot has none yet, so
they'd have to launch with arbitrary placeholder numbers. (1) is
implementable today (needs only an intraday peak-P&L tracker alongside the
existing `AccountState`/`RiskManager` plumbing) but was left out to avoid
adding a second, overlapping daily-loss mechanism before there's real data
to tell whether the fixed-%-of-equity limit or a give-back-from-peak limit
is the better fit for this account's actual volatility.

If/when revisited: (1) is the cheapest to add — track intraday peak
realized P&L in `AccountState` or `RiskManager`, add a
`should_flatten_for_giveback(snapshot)` alongside the existing
`should_flatten_for_loss_limit`, and wire it into `WarriorBot._check_flatten_triggers`
the same way. (2) and (3) both become feasible once `data/journal.sqlite3`
has enough `account_snapshots`/trade history to compute a trailing average
or an hour-of-day win-rate breakdown from.

## Deferred: candlestick shape as entry confirmation (not just exit)

The candlestick-pattern transcript describes a **bottoming tail** on the
pullback low as a bullish confirmation signal that increases confidence in
a breakout entry, and a large **topping tail** at the pullback peak as a
reason to be more cautious about taking the setup — explicitly framed as
soft confirmation ("not strictly required"), unlike the four hard pullback
gates that were implemented (VWAP hold, 9 EMA hold, volume profile, MACD).

**Status: not implemented.** `is_topping_tail()` exists (used for the
reversal *exit* signal in `PositionManager`) but there's no
`is_bottoming_tail()` and no wiring of either into entry confidence/sizing
for `bull_flag`/`abcd`. Would follow the same soft-boost pattern as the
catalyst and time-of-day multipliers if added.

## Deferred: two-stage validation rollout (alpha/beta gate before scaling)

The roadmap transcript's training progression (bulk simulator reps, then a
reduced-frequency "beta" phase requiring >60% win rate and net
profitability over ~10 trading days before scaling size/frequency) is
described as a project-management/rollout pattern, not a signal-generation
rule.

**Status: not implemented as code.** This is closer to an operating
practice for how the maintainer runs the bot (start at reduced size,
review the journal after a real sample, scale up deliberately) than a
strategy change. Could be operationalized later as a `RiskConfig`-style
"beta mode" (hard cap of N trades/day + reduced size until a rolling
win-rate/profitability threshold is met in the journal), mirroring the
existing profit-cushion sizing mechanism, if there's a desire to enforce
it in code rather than by discipline.

## Deferred: sector "heat" and Level 2 / order-book features

Both explicitly flagged in the source material as **not directly codable**
from the data this bot has:

- **Sector heat** (bonus preference for whatever sector is currently
  rotating hot — biotech, crypto, AI, etc.) needs a cross-sectional
  sector-momentum ranking across the whole market, not just per-symbol
  OHLCV.
- **Level 2 / order-book signals** (large resting buyer/seller detection,
  hidden/iceberg seller absorption, decelerating tape) need a market-depth
  data feed and tick-level time-and-sales, neither of which this bot
  currently subscribes to.

Three of the six source-material exit indicators fall in this bucket too
(large L2 seller, iceberg seller, decelerating tape) — the other three
(topping tail, red-after-green, volume burst) are implemented in
`PositionManager`'s reversal-exit check.

## Deferred: price "sweet spot" ($5-$10) and arbitrary tighter stops

- The roadmap transcript refines the $1-$20 price band to a $5-$10
  "personal sweet spot." Not implemented as its own filter or sizing
  factor — `gap_and_go`'s price band is unchanged. Could be added as
  another soft size multiplier (same shape as catalyst/time-of-day) if
  wanted.
- The candlestick transcript mentions using a tighter arbitrary stop
  (e.g. 10-15c) instead of the full structural stop when needed to
  preserve a 2:1 R:R against a realistic target. Not implemented — stops
  remain fully structural (pullback low / breakout level ± buffer) in
  every strategy.

## Deferred: trade rule-adherence tagging

The full-course transcript's metrics-review section suggests tagging each
trade with whether entry/exit rules were actually followed (not just
P&L), to separate "the strategy's inherent risk" losses from
"logic/implementation deviation" losses during review. Not implemented —
the journal records signals/decisions/orders/fills but has no
rule-adherence flag. Would need a defined "expected" entry/exit price to
diff the actual fill against. The execution/risk transcript's
"successful red day" framing (was the day successful by process, not just
P&L) and its rule-adherence-as-a-metric idea are the same underlying gap.

## Deferred: marketable-limit conversion for the kill switch and reversal exit

The execution/risk transcript makes explicit what earlier files implied:
IBKR rejects plain market orders outside regular trading hours
(4:00-9:30am / 4:00-8:00pm ET) — only limit orders work then. The
bracket's stop-loss was fixed (now a stop-limit order, see
`bracket_builder.py`), since that's a purely structural change (no live
market data needed). Two other order-placement paths still use a plain
`MarketOrder` with no `outsideRth` set and were deliberately left as-is
for now, at the maintainer's request:

- `utils/panic.py::flatten_all_positions` (the kill switch)
- `execution/position_manager.py::_reversal_exit`

**Why deferred:** converting these to the source material's "ask+10c /
bid-10c" marketable-limit pattern needs a live bid/ask quote fetched
immediately before placing the order (a real execution-layer addition —
a market-data snapshot call — not just a structural order-type change).

**If/when revisited:** add a small helper (e.g.
`broker/quotes.py::fetch_marketable_limit_price(ib, contract, action, offset_cents)`)
using `ib.reqMktData`/`ib.reqTickers` for a live bid/ask snapshot, then
swap both `MarketOrder` call sites for a `LimitOrder` at that computed
price. Until then, both paths risk being rejected or failing to fill if
triggered pre-market/after-hours — worth confirming against the live paper
Gateway during an actual premarket session.

## Deferred: green-to-red intraday flip circuit breaker

Distinct from (and simpler than) the 50%-give-back-from-peak rule above:
if today's P&L crosses into "green" territory (roughly a quarter-to-half
of the daily goal) and then flips negative, that specific transition is
treated as its own hard walk-away trigger. Unlike the give-back rule, this
one doesn't need historical trading data — it's buildable today with
`AccountState`/`RiskManager` alone. **Explicitly deferred at the
maintainer's request** ("skip for now"), not for a technical reason.

**If/when revisited:** track a `was_green_today` flag (set once
`daily_realized_pnl` crosses a configurable fraction of
`daily_profit_goal_usd`, cleared by `reset_daily_state()`), and add a
`should_flatten_for_green_to_red_flip(snapshot)` check to
`WarriorBot._check_flatten_triggers` alongside the existing EOD/loss-limit
triggers, reusing the same `panic_stop()` path.

## Deferred: ~30-second post-entry "instant resolution" check

The execution/risk transcript gives a concrete time window (~30 seconds)
for the "breakout or bailout" concept from earlier files: price should
move 2-3+ cents in the trader's favor within seconds of entry, or the
entry itself is treated as invalidated. **Explicitly deferred at the
maintainer's request.**

**Why deferred:** the bot operates on 1-minute bars throughout
(`fetch_warmup_bars`, the `keepUpToDate` live bar stream in `main.py`).
A ~30-second check needs sub-minute granularity. `broker/market_data.py::subscribe_real_time_bars`
(5-second real-time bars) already exists but has never been wired into
`main.py` — it's dead code today. Wiring it up is a real architecture
change (a second, faster bar stream alongside the existing 1-minute one,
with its own aggregation/state), not a quick add-on.

## Not applicable to an automated system

From the execution/risk transcript: trading-station hardware (laptop/monitor
budget), hotkey scripting, Level 2 color-coding preferences, and the
"ladder view" — all manual-execution-UI concerns with no equivalent in an
automated bot. The `weekly_goal = daily_goal × 3` / monthly goal cascade is
a reporting/target-setting formula, not a live-trading gate — could be
added to `scripts/daily_report.py` later if useful, but doesn't belong in
the trading logic itself.
