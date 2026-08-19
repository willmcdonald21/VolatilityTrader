# Strategy enhancement decisions

Source material: Warrior Trading transcripts in the maintainer's local
notes folder (`warrior_trading_strategy_notes.md`, `warrior_trading_roadmap_notes.md`,
`warrior_trading_full_course_notes.md`, `warrior_trading_candlestick_pattern_notes.md`,
`warrior_trading_execution_risk_notes.md`, `warrior_trading_dip_or_dump_notes.md`,
`warrior_trading_candlestick_deep_dive_notes.md`, `warrior_trading_three_concepts_notes.md`,
`warrior_trading_5_failure_causes_notes.md`).
This file tracks findings from that material that were considered but
**not** implemented, and why, so the reasoning isn't lost. Implemented
findings are just... implemented; see `bull_flag.py`/`abcd_pattern.py`
(pullback validity gates), `pullback_validity.py`, `indicators.py` (MACD,
candlestick shape detectors, first-lower-low, `resample_bars` for
multi-timeframe checks), `base_strategy.py` (`_check_engaged` --
stock-level 9/20 MACD disengagement gate; `SymbolContext.scanner_rank`),
`position_manager.py` (reversal exit, stop-limit price tracking),
`bracket_builder.py` (stop-limit order type), `risk_manager.py` (catalyst,
time-of-day, and "obviousness" size boosts), and `config.yaml`.

## Implemented: "5 Leading Causes of Trader Failure" additions

Psychology/risk-management focused, but two of its five causes give
concrete, mechanically implementable rules (unlike file 8's vaguer
"regime" concept, which explicitly had no formula given):

- **Daily "starter position" regime protocol (Cause #2).** Fully
  mechanical rule: take smaller size on the day's first trade; if it
  loses, treat that as a cold-market caution flag and cap size for the
  rest of the session. Implemented in `RiskManager`:
  `risk.starter_trade_size_multiplier` (default 0.5) shrinks the very
  first accepted trade of the day (`_trades_accepted_today == 0`);
  `risk.starter_trade_downgrade_multiplier` (default 0.5) applies to every
  subsequent trade for the rest of the session if
  `AccountState.first_closing_trade_pnl()` (new method -- the realized
  P&L of the session's earliest closing fill) comes back `<= 0`. Both
  reset via the existing `mark_start_of_day`/`reset_daily_state` path.
  This is also the mechanical fix for Cause #3 (discipline decay): the
  transcript's own framing is that a human's natural response to an early
  loss is to *increase* size to make it back, exactly backwards from the
  regime-reading logic -- an automated system has no emotional override
  problem, so encoding the rule mechanically was the direct ask, not an
  extrapolation.
  `first_closing_trade_pnl()` is an approximation, not exact trade
  grouping: a position closed via a scale-out then a later stop-out
  produces two closing fills, and only the earlier one's sign is used.
  Matches the source material's own binary framing ("did the starter
  trade work or not") closely enough without needing full round-trip
  grouping logic.
- **Market breadth as a regime signal (Cause #2).** "Count of stocks up
  >100% on the daily leading-gainers scan" -- added
  `warrior_bot/scanner/regime.py::count_extreme_gainers()`, logged once
  per scan cycle (only when it changes, to avoid log spam at the
  5-second scan cadence) in `WarriorBot._scan_loop`. **Deliberately
  logged only, not wired into sizing**: this bot's own scanner
  (`scanner.above_price`/`below_price`) only ever sees onboarded
  candidates within the configured price band, so a genuinely hot day
  with big moves concentrated in higher-priced names could still read as
  "zero extreme gainers" here -- a systematic undercount bias that would
  make an automatic sizing lever actively wrong on some genuinely strong
  days. Kept as a loggable number for the maintainer to eyeball each
  morning instead.
- **Tail-risk / worst-single-trade-R as a distinct tracked metric
  (Cause #4).** `scripts/daily_report.py` now prints a "Worst R" column
  per strategy alongside the existing average R -- directly encodes the
  transcript's point that an average P/L ratio can look fine while still
  masking an occasional 10-20x-normal loss that did real account damage
  the average alone wouldn't reveal.

**Confirmed already correct, no change needed:**
- **Cause #5 (maintain multiple pre-validated strategy variants)**
  reinforces the same "momentum trading as overarching framework with
  pluggable sub-strategies" confirmation already made for file 8 --
  `strategies.{gap_and_go,bull_flag,abcd,vwap_reversion}.enabled` already
  provides this. No new confirmation needed beyond noting the
  reinforcement.

**Deferred:**
- **Cause #1's diminishing-returns/slippage modeling** (position size
  scaling into 1,000s-10,000s of shares eventually hits liquidity/slippage
  limits relative to a stock's own volume) is not applicable at this
  bot's current configured scale -- `risk.max_shares_per_trade` (2,000)
  and `risk.max_position_notional_usd` ($5,000) already keep it well
  inside "no meaningful slippage" territory for a small account, and
  there's no backtesting engine in this codebase to model diminishing
  returns against in the first place (per the README: the SQLite journal,
  not a backtest, is the primary feedback loop). Worth revisiting only if
  the account ever scales enough for this to become a real constraint --
  would need either historical execution-quality data (fill price vs.
  quote at signal time) or a backtest engine, neither of which exist yet.

## Implemented: "Dip or Dump" entry-time dump checklist

The dip-or-dump transcript's dump-checklist items map almost entirely onto
functionality that already existed for `PositionManager`'s reversal *exit*
(`is_topping_tail`, `is_high_volume_red_bar` in `indicators.py`) or was
already enforced by `validate_pullback` (9 EMA hold, MACD, ≥50% retracement
via each strategy's own pullback-depth config, lighter pullback volume).
What was missing was applying the topping-tail/high-volume-red-bar checks
at *entry* time, not just exit time, plus two genuinely new pieces:

- **Topping tail / high-volume red bar within the pullback itself** now
  reject the entry (`pullback_validity.py::validate_pullback`, gated by
  `pullback_quality.reject_topping_tail` /
  `pullback_quality.reject_high_volume_red_bar`). Same underlying
  detectors as the reversal exit, same thresholds by default
  (`topping_tail_wick_ratio: 2.0`, `high_volume_red_bar_multiple: 2.0`) --
  a dump signal means the same thing whether you're about to enter or
  already in the trade.
- **"Obviousness" (top-N leading % gainer)** — `scan_candidates` already
  returns symbols ordered by the scanner's own rank (it's a
  `TOP_PERC_GAIN` scan), so no new data source was needed: `main.py`'s
  scan loop now captures each symbol's rank at onboarding time
  (`SymbolContext.scanner_rank`, mirroring how `catalyst_category` is
  captured once, not re-checked per bar) and `RiskManager` applies a soft
  size multiplier (`risk.obvious_rank_threshold`,
  `risk.obvious_size_multiplier`) when a signal's symbol was top-3 ranked
  at onboarding -- same treatment as the catalyst/time-of-day boosts, not
  a hard reject. The source material's stated exception (fresh breaking
  news can matter before a stock is top-3 ranked) already exists
  independently via the catalyst size boost, so no extra logic was needed
  for it.
- **5-minute multi-timeframe confirmation** — the transcript's explicit
  new detail vs. prior files: check the 5-minute chart's MACD/topping-tail
  state as a veto layer even when the faster entry-timeframe signal looks
  clean. Implemented as `indicators.py::resample_bars()` downsampling the
  existing 1-minute `ctx.bars` into 5-minute buckets (no new IB
  subscription -- this only needed *downsampling* data the bot already
  has, unlike the sub-minute upsampling the 30-second check below would
  need), then re-running the same MACD/topping-tail checks on the
  resampled bars (`pullback_quality.require_5m_macd_confirmation` /
  `reject_5m_topping_tail`).

**Confirmed already correct, no change needed:**
- Dump items "breaks 9 EMA" and "MACD crosses negative" were already
  enforced by `validate_pullback` and `_check_engaged`.
- Dump item "retraces >50% of the move" is already `bull_flag`'s
  `max_pullback_pct: 50.0`. `abcd`'s `max_bc_pullback_pct: 60.0` looks like
  a mismatch but isn't a bug -- ABCD is deliberately a deeper-pullback
  pattern than bull_flag by design (see `abcd_pattern.py`'s own
  docstring), not the same generic "prior move retracement" the
  dip-or-dump transcript's 50% rule describes; left as-is.
- "Micro pullback" — the transcript's own codable takeaway is that this
  is the *same* pullback logic as `bull_flag`/`abcd`'s first-pullback
  detection, just needing finer-than-1-minute resolution on the fastest
  movers. `min_consolidation_bars: 1` already treats a single-bar pullback
  as the ideal case, not an edge case -- confirms no separate pattern
  logic was ever needed at 1-minute resolution.
- "No-trade on ambiguity" tie-breaker rule — `validate_pullback` already
  rejects on the *first* failing gate (a strict AND of all checks, no
  weighted score), which is already the transcript's stated correct
  behavior for mixed/borderline signals. No change needed.

**Scope note:** the new pullback-quality checks and 5-minute veto apply to
`bull_flag`/`abcd` only, via the shared `validate_pullback` gate --
consistent with its existing docstring ("both are spike then pullback
then breakout patterns"). `gap_and_go` (opening-range breakout, not a
discrete spike-then-pullback) and `vwap_reversion` (mean-reversion) don't
share that shape and weren't touched. The scanner-rank size boost, being
symbol-level rather than pattern-specific, applies to all four strategies
uniformly through `RiskManager`.

## Implemented: candlestick deep-dive additions (wick-weighted strength, pairwise volume, graduated retracement)

Three genuinely new, well-scoped pieces from the "only candlestick pattern"
transcript, plus several items that turned out to already be true of the
existing design:

- **Wick-weighted breakout-candle strength.** The transcript's core nuance
  ("a green candle with a large upper wick is *not as bullish as it
  looks* -- sellers pushed it back down within that period") is mostly
  already covered for *pullback* bars by the existing `is_topping_tail`
  entry gate, but nothing checked the shape of the *breakout/trigger*
  candle itself -- a breakout that barely closes above the level with a
  large upper wick is a weak signal by the same logic. Added
  `indicators.py::candle_strength(bar)` -- signed `(close-open)/(high-low)`
  in `[-1, 1]` -- and a `min_breakout_candle_strength` gate (default `0.0`,
  i.e. the breakout bar must close net-bullish, not merely above the
  level) applied at the entry-trigger point in all three breakout-style
  strategies: `gap_and_go.py`, `bull_flag.py`, `abcd_pattern.py`.
  `vwap_reversion`'s entries are bounce/reversion shaped rather than a
  single "breakout candle," so it was left out, consistent with the
  existing scope note below.
- **Precise pairwise volume-profile rule.** The existing aggregate check
  (`sum(pullback volume) < sum(up-move volume)`) can still pass while one
  individual red bar in a multi-bar pullback outweighs the *specific*
  green candle immediately preceding the pullback -- the transcript's
  worked CZ/CZOO counter-example is exactly this shape. Added
  `pullback_quality.require_pullback_lighter_than_prior_green_bar`:
  compares each pullback bar's volume against `up_move_bars[-1]` (the
  actual anchor green/spike candle) rather than an aggregate or a generic
  rolling average.
- **Graduated retracement confidence.** The hard `bull_flag.max_pullback_pct`
  (50%) ceiling already existed; this transcript adds an explicit
  *preference* within that valid range ("I'd rather see it hovering in the
  top 25% of the move"). Rather than inventing a continuous confidence
  formula (which would be stylistically inconsistent with every other
  soft signal in this file, all implemented as threshold + multiplier),
  added `risk.shallow_pullback_threshold_pct` (25%) /
  `shallow_pullback_size_multiplier` to `RiskManager`, keyed off
  `bull_flag`'s existing `pullback_pct` signal-context field -- same
  discrete boost-on-condition treatment as catalyst/time-of-day/
  obviousness. Scoped to `bull_flag` only: `abcd`'s pullback is a
  deliberately deeper pattern by design (`min_bc_pullback_pct: 20.0`), so
  "shallower is better" doesn't apply the same way there, and the
  transcript itself frames ABCD as the presenter's non-favorite pattern.

**Confirmed already correct, no change needed:**
- **Position size vs. dollar-risk independence.** The GX trade example
  (95% of buying power deployed, but only ~$300-400 actual dollar risk)
  describes exactly how `RiskManager._size_position` already works:
  `dollar_risk_budget = net_liquidation * risk_per_trade_pct`, shares
  sized from *that* divided by stop distance, with position notional
  capped only by the separate hard ceilings
  (`max_position_notional_usd`/`max_shares_per_trade`/buying power) --
  never as a fixed fraction of equity tied to risk-per-trade. No tension
  with file 5's starter-size ramp either: that's a *daily* ramp
  (`risk.cushion_size_fraction`, already implemented), a different,
  independently-configurable layer from per-trade stop-distance sizing.
- **Multi-timeframe veto.** The GX trade's real example (10-second chart
  showing a valid micro pullback while the 1-minute chart showed a doji,
  presenter declined to add) is a real-world instance of exactly the
  5-minute MACD/topping-tail veto already implemented (see the "Dip or
  Dump" section above) -- no new mechanism needed, just confirms the
  existing one models a real trading decision correctly.
- **Micro pullback vs. bull flag terminology.** Confirms file 6's own
  speculation: same detection logic, timeframe-only naming difference
  (micro pullback <=1min, bull flag >=5min). `bull_flag.py`'s docstring
  now notes explicitly that since this bot only ingests 1-minute bars,
  every signal it produces is technically a "micro pullback" per this
  transcript's own definition -- the class is kept named `bull_flag` for
  continuity with the rest of the codebase, not as a timeframe claim.
- **ABCD / head-and-shoulders deprioritization.** Matches existing scope
  exactly -- ABCD exists but is documented elsewhere as not the primary
  pattern; head-and-shoulders was never implemented and this transcript
  gives no new reason to.

## Implemented: "3 Concepts" additions (200 EMA, minimum sample size)

Mostly a psychology/meta-strategy transcript, light on new mechanical
rules -- most of its codable content turned out to already match the
existing design (see confirmations below). Two small, genuine additions:

- **`SymbolContext.ema_200`** -- this transcript is the most explicit,
  authoritative statement of the trader's complete indicator set (9/20/200
  EMA + VWAP + MACD + volume + candlesticks, "resist the urge to add
  indicators beyond this set"). 9 and 20 EMA were already implemented;
  200 wasn't. Added as a plain property (`base_strategy.py`), same
  None-safe pattern as `ema_9`/`ema_20`. **Deliberately not wired into any
  gate**: the source material never gives a concrete rule for *how* 200
  EMA should filter a decision (unlike the 9 EMA pullback-hold gate, which
  has an explicit rule), and practically, `fetch_warmup_bars` only pulls
  60 minutes of history -- a 200-period EMA on 1-minute bars needs 200
  bars of warm-up, so it would read `None` for most of this bot's actual
  7-10am ET trading window regardless. Exposed for future
  context/journaling use, not invented into a filter that isn't actually
  in the source material.
- **Minimum sample size for strategy evaluation.** Explicit rule: don't
  judge whether a strategy has a real edge from fewer than ~100 trades.
  `scripts/daily_report.py` now flags any strategy with under 100 trades
  in the journal as too small a sample to draw conclusions from yet
  (`MIN_TRADES_FOR_CONCLUSIONS`). Kept to the reporting script only --
  purely informational, doesn't touch live trading behavior.

**Confirmed already correct, no change needed:**
- **Pluggable sub-strategies under a top-level momentum framework.** The
  transcript's "momentum trading as the overarching strategy, with
  sub-strategies like gap-and-go turned on/off by condition" already
  describes this bot's actual architecture --
  `strategies.{gap_and_go,bull_flag,abcd,vwap_reversion}.enabled` are
  already independently toggleable per `StrategiesConfig`.
- **Rule-adherence tagging** (separating "the strategy has no edge" from
  "execution didn't match the specified rules" as distinct failure
  diagnoses) is the same gap already tracked in the "Deferred: trade
  rule-adherence tagging" section below -- this file reinforces it with a
  cleaner framing but doesn't change the deferral.
- **Candlestick/sentiment priority over indicators.** Directionally
  consistent with the existing design already: candle-shape checks
  (`is_topping_tail`, `candle_strength`, the pairwise volume rule) and
  indicator checks (VWAP/9EMA/MACD) are both implemented as hard
  entry-time gates in `pullback_validity.py`, not as a formally weighted
  scoring system with one prioritized over the other -- there's no
  concrete, contradicting evidence this transcript gives that would
  require restructuring that into an explicit priority order.

## Deferred: regime-dependent position sizing ("hot" vs. "cold" market)

Explicit new principle: trade more aggressively in a "hot" market, ease
off in a "cold" one -- explicitly described by the presenter as
"educated intuition" with **no formula given** for how much to scale by
regime.

**Status: not implemented, and deliberately not guessed at.** Same bucket
as the "sector heat" deferral above -- both need a market-wide,
cross-sectional signal this bot doesn't currently compute (this bot only
tracks per-symbol state in `SymbolContext`, not an aggregate view of "how
many candidates are qualifying today" or "what's average relative volume
across the whole scanner list right now"). The source material itself
offers no concrete threshold or formula, only named candidate proxies as
possibilities (count of five-pillar-qualifying candidates per day,
aggregate scanner-wide relative volume, realized volatility) --
implementing any one of these would mean inventing a rule not actually
specified by the transcripts, which risks adding an untested heuristic
sizing lever to live trading. If revisited, the cheapest starting proxy
given what's already computed is probably a rolling count of symbols
onboarded per session (`WarriorBot._onboard_symbol` already logs this
implicitly) compared against a trailing baseline -- but this needs a
deliberate design/tuning pass, not a drive-by addition alongside a
strategy-notes ingestion pass.

## Deferred: an actual 5-minute-resolution "bull flag" detection pass

The transcript's codable takeaway is that a single pullback-detection
function, parameterized by timeframe, covers both micro-pullback and bull
flag -- today, `BullFlagStrategy` only ever runs against the live
1-minute bar stream. Running the *same* logic against `resample_bars(ctx.bars,
bucket_minutes=5)` as a second, independent detection pass (genuinely
producing "bull flag" signals on a slower timeframe, not just the existing
5-minute *veto* layer) is deferred -- unlike the veto (a single boolean
check), a full second pass raises real design questions not addressed by
this transcript: does it need its own `state["triggered"]` tracking
independent of the 1-minute pass, its own risk sizing treatment, and
should it fire concurrently with a 1-minute signal on the same symbol or
supersede it. Worth doing deliberately if/when there's a concrete reason
to want slower-timeframe bull-flag signals specifically, not as a
drive-by addition.

## Deferred: sub-minute (10-second) micro-pullback resolution

The dip-or-dump transcript states the fastest-moving stocks' pullbacks can
be too brief to see on a 1-minute chart, needing a 10-second chart to
trade them at all. Unlike the 5-minute multi-timeframe confirmation above
(which only needed *downsampling* existing 1-minute bars), this needs
*finer* granularity than the bot currently ingests -- the same
architecture gap as the existing "~30-second post-entry instant
resolution check" deferral below. **Status: not implemented**, for the
same reason: `broker/market_data.py::subscribe_real_time_bars` (5-second
real-time bars) exists but has never been wired into `main.py`. Both
deferred items would be solved by the same future work (a second,
faster bar stream alongside the existing 1-minute one) -- worth doing
together if/when revisited rather than as two separate efforts.

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

The "3 Concepts" transcript gives a more specific version of the same idea,
with concrete numbers: backtest -> **>=6 weeks** of simulator/paper trading
demonstrating consistent profitability -> switch to live trading at
**minimal size** (explicit example: 5-10 shares) with a **trivial initial
profit target** (~$10/day) -> scale size gradually (10 -> 20 -> 50 -> 100+
shares) only as consistency holds at each tier. Framed explicitly as
*emotional* conditioning to real money, not skill-building -- the skill is
already assumed proven in sim; the ramp exists so a large first real loss
doesn't happen before the trader has adapted to the psychological weight
of real gains/losses. Also gives an explicit minimum-sample-size rule for
judging whether a strategy variant actually has an edge at all: **>=100
trades** before drawing conclusions -- implemented as a note in
`scripts/daily_report.py` (flags any strategy under 100 trades in its
journal as too small a sample to judge yet), since that piece is cheap,
purely informational, and doesn't touch live trading behavior.

**Status: the staged live-size rollout itself is still not implemented as
code**, for the same reason as before -- this is an operating practice for
how the maintainer runs the bot (start at reduced size, review the journal
after a real sample, scale up deliberately), not a strategy change, and
previous sessions have kept this deliberately as a human-followed
discipline rather than an automated gate. Could be operationalized later
as a `RiskConfig`-style "beta mode" (hard cap of N trades/day + reduced
size until a rolling win-rate/profitability threshold is met in the
journal, now with concrete candidate numbers from this file: 6 weeks/~100
trades minimum sample, then a small-share-count floor before scaling)
mirroring the existing profit-cushion sizing mechanism, if there's ever a
desire to enforce it in code rather than by discipline.

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
  currently subscribes to. Reaffirmed by the dip-or-dump transcript's
  "large sell orders / pegged orders" dump indicator -- a pegged order
  specifically requires observing an order's price re-pegging *over time*,
  not just a size snapshot, which needs streaming order-book data this bot
  has no plan to subscribe to yet.

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
