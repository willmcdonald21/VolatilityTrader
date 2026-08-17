# Strategy enhancement decisions

Source material: four Warrior Trading transcripts in the maintainer's local
notes folder (`warrior_trading_strategy_notes.md`, `warrior_trading_roadmap_notes.md`,
`warrior_trading_full_course_notes.md`, `warrior_trading_candlestick_pattern_notes.md`).
This file tracks findings from that material that were considered but
**not** implemented, and why, so the reasoning isn't lost. Implemented
findings are just... implemented; see `bull_flag.py`/`abcd_pattern.py`
(pullback validity gates), `pullback_validity.py`, `indicators.py` (MACD,
candlestick shape detectors), `position_manager.py` (reversal exit),
`risk_manager.py` (catalyst and time-of-day size boosts), and `config.yaml`.

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
diff the actual fill against.
