# Strategy enhancement decisions

Source material: Warrior Trading transcripts in the maintainer's local
notes folder (`warrior_trading_strategy_notes.md`, `warrior_trading_roadmap_notes.md`,
`warrior_trading_full_course_notes.md`, `warrior_trading_candlestick_pattern_notes.md`,
`warrior_trading_execution_risk_notes.md`, `warrior_trading_dip_or_dump_notes.md`,
`warrior_trading_candlestick_deep_dive_notes.md`, `warrior_trading_three_concepts_notes.md`,
`warrior_trading_5_failure_causes_notes.md`, `warrior_trading_raw_candlesticks_notes.md`,
`warrior_trading_ta_master_class_notes.md`, `warrior_trading_dip_buying_notes.md`,
`warrior_trading_leverage_mechanics_notes.md`).
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

## Implemented: "Dip-Buying Methodology" additions

This transcript's own explicit dip-vs-reversal checklist (4 required + 2
bonus checks) turned out to already be ~90% implemented across earlier
sessions -- pullback-lighter-than-preceding-green-candle (file 6's
`require_pullback_lighter_than_prior_green_bar`), MACD-must-be-positive
(already the base MACD gate), and round-number proximity (file 11's
`round_number_breakout`, though framed as *crossing* a level rather than
this file's *proximity to* one -- close enough in spirit not to need a
second, overlapping check) were all already there. Two genuine gaps
surfaced by checking each item against the actual code:

- **Rising volume on the advance.** The existing volume checks
  (`require_pullback_lighter_than_prior_green_bar`, the aggregate
  pullback-vs-up-move comparison) all compare the *pullback* against the
  *up-move* -- none of them checked whether the up-move itself was
  volume-confirmed. Added `indicators.py::has_rising_volume_on_advance()`
  (second-half vs. first-half average volume across the up-move bars, not
  strict bar-by-bar monotonicity, which would be too brittle against
  normal noise) and wired it into `validate_pullback` as a new hard gate,
  `pullback_quality.require_rising_volume_on_advance` (default on) --
  matches the "required check" framing of the other three checklist items
  already implemented as hard gates in `pullback_validity.py`, not the
  "bonus" (soft) framing used for the L2/round-number checks.
- **9 EMA gate was stricter than the source material describes.** The
  existing check used `pullback_low < ema_9` (any bar's *low*, i.e. any
  wick at all, would invalidate the whole pullback). This transcript is
  explicit that "a brief single-candle wick below the 9 EMA that
  immediately reclaims it" should be tolerated as noise -- only a bar
  that actually *closes* below the EMA is a real break. Changed to
  `any(b.close < ema_9 for b in pullback_bars)`. Deliberately left the
  VWAP check on the same line using `pullback_low` (low-based) unchanged
  -- the source material's wick-tolerance nuance is specific to the 9
  EMA, not stated for VWAP. Confirmed via the full test suite that this
  strictly-more-permissive change didn't flip any existing "should
  reject" fixture to "passes" (the existing 9-EMA rejection test's
  fixture rejects on both the old low-based and new close-based logic, so
  it wasn't accidentally testing wick-tolerance by coincidence).

**Confirmed already correct, no change needed:**
- **Iceberg/hidden-seller detection** and the L2 "large resting seller"
  bonus check both need tick-level time-and-sales/order-book data this
  bot doesn't subscribe to -- the same gap already tracked under
  "Deferred: sector heat and Level 2 / order-book features" below;
  reinforced, not new.
- **Tail-risk tracking** (a single outsized loss can distort the
  aggregate P/L ratio) is exactly what `scripts/daily_report.py`'s
  worst-single-trade-R column (added for file 9) already surfaces.
- **Breakout vs. dip as separate sub-strategy win-rate/R:R profiles**:
  already how the bot is structured -- `gap_and_go` (breakout-style) and
  `bull_flag`/`abcd` (pullback-style) are independently configured
  strategies with their own tunables, not pooled into one signal type.
- **Alternative (60-65% win rate, ~1:1 P/L) target profile** as a more
  realistic early-stage benchmark than the 2:1 framing used elsewhere:
  purely informational for judging `daily_report.py`'s existing win% and
  avg-R output against; doesn't need its own code, since both numbers are
  already surfaced and a second hardcoded "pass/fail against benchmark
  X or Y" column wouldn't add information beyond what's already visible.

**Deferred (substantial new subsystems, not drive-by additions):**
- **Scale-in / add-to-winners position building** (starter size -> add on
  each subsequent new-high confirmation -> immediate full exit on stop,
  rather than the current single-shot entry). This is a materially
  different order-management model than what exists today: every
  strategy currently produces exactly one signal -> one bracket order ->
  one stop/target for the life of a position (`OrderManager`,
  `PositionManager`). Building genuine scale-in support would need new
  state (how many adds so far, blended cost basis, a re-evaluation path
  for *already-open* positions rather than just fresh signals) and
  changes how `RiskManager`'s risk-per-trade sizing model works (which
  currently assumes one entry price and one stop per position). Comparable
  in scope to the already-deferred 5-minute bull-flag detection pass and
  trend-line/S/R detection -- worth a deliberate design pass, not
  something to bolt on inside a strategy-notes ingestion session.
- **Graduated give-back circuit breaker and regime-linked size-cap
  toggle** -- see the enriched "Deferred: daily 'give-back' circuit
  breaker" section below; still explicitly deferred at the maintainer's
  request, now with this file's concrete numbers recorded for whenever
  it's revisited deliberately.

## "Leverage Mechanics & Small-Account Day-One Walkthrough" additions

This transcript's new content is a statistical justification for the gain%
filter, worked leverage/buying-power numbers, one new named exit pattern,
an L2 concept distinct from the single-large-seller signal already
deferred, and named terminology ("resulting") for a bias the journal
review framework already guards against structurally. No net-new,
concretely-specified, low-risk rule surfaced -- everything below is either
a reinforcement of an existing implementation or a deferral for the same
reason as an already-deferred item.

**Confirmed already correct, no change needed:**
- **The >=10% gain filter as outlier detection, not an arbitrary cutoff.**
  The transcript's explicit statistical framing (most stocks trade within
  a normal +/-4-5% daily range; a >10% mover is a rare statistical outlier,
  typically only ~5-10 out of the whole tradeable universe on a given day)
  is exactly what `gap_and_go.min_gap_pct: 10.0` already encodes
  (`config.yaml`) -- this file adds the *why*, not a new threshold. Same
  reinforcement for `min_rel_volume: 5.0` (all four strategies,
  `config.yaml`): this transcript restates the "90% of my profit comes
  from >5x relative volume" stat cited in an earlier session, confirming
  that stat's "5x" reading (not a literal "500x") is the one already coded.
- **Leverage/buying-power sizing model.** The transcript's worked example
  (6x leverage: $1,000 cash -> $6,000 buying power; risk is a function of
  stop distance, not notional size; buying power, not equity, gates max
  position size) describes exactly the two-quantity model already in place:
  `AccountState.snapshot()` (`account_state.py`) polls `net_liquidation`
  and `buying_power` as two independently-reported IBKR values, never one
  derived from the other. `RiskManager._size_position` (`risk_manager.py`)
  computes `dollar_risk_budget` from `net_liquidation` (equity) via
  `risk_per_trade_pct`, entirely independent of `cap_by_buying_power =
  buying_power / entry_price`, which caps notional position size
  separately. This is the same "position size vs. dollar-risk
  independence" principle already confirmed for the (unleveraged) GX
  example under "Implemented: candlestick deep-dive additions" above --
  this transcript's contribution is a leveraged small-account worked
  example of the identical mechanic, not a new one.
- **Stair-stepping at half-/whole-dollar levels** (the NEXI $19.00 ->
  $19.50 -> $20.00 walkthrough) is the same behavior
  `round_number_breakout` already captures (see "Implemented: 'TA Master
  Class' additions" below) -- another live-trade instance of an existing
  signal, not a new one.
- **"Resulting" bias** (Annie Duke's term for judging a decision by its
  outcome instead of its process) is named terminology for the same gap
  already tracked under "Deferred: trade rule-adherence tagging" below --
  see that section for the reinforcement; no new deferral needed.
- **"Stacked sellers"** (many moderate resting sell orders across several
  price levels near resistance, vs. one large order at one level) is a
  variant of the same L2/order-book gap already tracked under "Deferred:
  sector heat and Level 2 / order-book features" below -- see that section
  for the reinforcement; no new deferral needed.

**Deferred:**
- **"Jackknife" exit indicator** (a squeeze-up that reverses sharply within
  the same or immediately next bar -- faster/sharper than an ordinary
  topping tail). Conceptually a stricter, graduated-severity variant of
  the already-implemented `is_topping_tail()` (`indicators.py`), but the
  transcript gives no numeric wick-ratio or bar-count threshold to
  distinguish "jackknife" from an ordinary topping tail -- same category of
  risk already avoided for "regime-dependent sizing" and the deferred
  trend-line/S/R work above (implementing a threshold not actually
  specified in the source material would be inventing an untested
  heuristic, not extracting one). The "speed" dimension specifically would
  also need sub-minute resolution to distinguish from a same-bar-only
  shape check, which is the same architecture gap as the existing
  "Deferred: sub-minute (10-second) micro-pullback resolution" section.
  **Status: not implemented.** If/when revisited with a concrete threshold,
  the natural home is `PositionManager._check_reversal_exit`
  (`position_manager.py`) alongside the existing `topping_tail` reason, at
  a stricter `wick_ratio` than the default `2.0`.

## Implemented: "TA Master Class" additions

This is the broadest-scoped transcript so far (trend-line detection,
horizontal S/R clustering, a three-pattern hierarchy, a decision-vs-outcome
grading framework, and stop-placement-by-nearest-support). Most of it is
either a substantial new subsystem better deferred deliberately than built
in a drive-by pass, or already true of the existing architecture. One
piece was concretely specified and low-risk enough to implement now:

- **Psychological round-number breakout confirmation.** Source material's
  clearest, most quantifiable new rule: half-dollar levels matter below
  ~$10, whole-dollar levels at/above, with $1.00 called out as
  particularly significant for low-priced stocks ("very hard... to break
  and hold over $1"). Added `indicators.py::round_number_increment()` /
  `crossed_round_number()` and wired into all three breakout-style
  strategies (`gap_and_go`, `bull_flag`, `abcd`): if the breakout candle's
  close crossed a round-number level that the immediately preceding bar's
  close was still under, `round_number_breakout` is set in the signal's
  context, and `RiskManager` turns it into a soft size boost
  (`risk.round_number_size_multiplier`) -- same discrete
  threshold-and-multiplier treatment as every other soft signal in this
  file. Deliberately scoped to *crossing* a level (a concrete, binary,
  well-defined event), not proximity to one, which would need an
  arbitrary "how close counts" tolerance the source material doesn't
  specify.

**Deferred (substantial new subsystems, not drive-by additions):**
- **Trend-line detection** (ascending/descending support and resistance,
  fit from sequential pullback lows / rally highs) and **horizontal S/R
  clustering with "broken level flips role"** state tracking. Both are
  genuinely new capabilities, not extensions of an existing gate:
  trend-line fitting needs a line-fit (even a simple two-point fit) across
  `swing_points()` output (which already exists as a building block) with
  a defined tolerance band; horizontal-level clustering needs grouping of
  prior highs/lows with round-number weighting; "broken level flips role"
  needs new *persistent per-symbol state* tracked across the session
  (which levels are currently classified support vs. resistance, and
  when a break should be considered to have "held" long enough to
  reclassify). None of this is concretely specified precisely enough in
  the source material to implement safely in a single pass (e.g. no
  stated tolerance band for what counts as "connecting" two pullback
  lows, no stated bar-count for how long a break must "hold" before
  flipping role) -- worth a deliberate design pass if/when wanted, not a
  guessed implementation bundled into a strategy-notes ingestion session.
- **Stop-placement by nearest valid support** (when multiple candidate
  stop levels exist -- pullback low, EMA, VWAP, horizontal S/R, trend
  line -- prefer whichever is closest to entry). **Confirmed the core
  principle is already followed**: every strategy already stops at the
  single tightest legitimate structural level for its own pattern
  (`bull_flag`/`abcd` at the pullback/C low, `gap_and_go` at the breakout
  level) -- these aren't arbitrary distances. **Declined to extend this
  to a multi-candidate comparison** (e.g. swap in `ctx.ema_9`/`ctx.vwap`
  when tighter than the pullback low): a stop placed at a level that
  wasn't the one actually invalidating the pattern risks *more* noise
  stopouts, not fewer, and the source material doesn't specify a
  validity/ranking rule for when an alternate candidate is safe to use
  instead of the pattern's own structural level. Implementing this
  without that rule would be inventing an untested stop-placement
  heuristic, the same category of risk already avoided for the "market
  heat" and "nearest support" ideas in earlier sessions.

**Confirmed already correct, no change needed:**
- **Decision-quality vs. outcome-quality / process-adherence grading.**
  Structurally guaranteed for this bot in a way it isn't for a human
  discretionary trader: a signal only ever exists when a strategy's own
  coded conditions actually fired, so there is no code path for a
  "rule-violating trade that happened to work" the way a human can
  override their own plan -- process adherence is 100% by construction
  for every trade this bot places (modulo bugs). This is a different
  question from the existing "Deferred: trade rule-adherence tagging"
  section below (which is about whether the *rule set itself* is well
  calibrated, judged from aggregate journal review) -- this file
  reinforces that existing deferral rather than replacing it or adding a
  new gap.
- **Three-pattern hierarchy (bull flag > ABCD > micro pullback).**
  "Micro pullback" and "bull flag" are the same code path today (per the
  `bull_flag.py` docstring note from an earlier session -- this bot only
  ingests 1-minute bars, so every signal is technically a micro
  pullback), so a 3-way preference ranking doesn't map onto 3 distinct
  strategies the way it would for a human discretionarily choosing
  between chart timeframes. The `bull_flag` > `abcd` half of the ranking
  is a genuine, distinct preference, but -- unlike the round-number
  signal above -- it's a subjective "which pattern do I trust more in
  general" preference rather than an objective structural property of a
  specific setup, and giving `abcd` a blanket smaller-size treatment
  purely for being pattern #2 would be a values call about capital
  allocation, not a mechanical rule extracted from the transcript. Left
  unimplemented rather than guessed at; each strategy's own independently
  tuned pullback-depth/quality config already reflects that they're
  different, deliberately-scoped patterns.

## Implemented: "Reading Raw Candlesticks" additions

This transcript's central thesis (raw candlesticks/tick data > computed
indicators, argued from a data-hierarchy + lag standpoint) mostly
reinforces architecture already confirmed in prior files' sessions. Two
genuinely new pieces, both filling gaps this file's own concepts pointed
at directly:

- **`is_bottoming_tail()`** (`indicators.py`) -- the exact mirror of the
  existing `is_topping_tail()` (long lower wick vs. long upper wick),
  matching this file's "hammer" description. This was explicitly flagged
  as a gap in an earlier session ("Deferred: candlestick shape as entry
  confirmation (not just exit)" -- `is_topping_tail()` existed for the
  exit signal but there was no bullish-confirmation counterpart). Wired
  into `bull_flag.py`/`abcd_pattern.py`: a bottoming tail on the pullback's
  low bar sets `bottoming_tail_confirmation` in the signal's context,
  which `RiskManager` turns into a soft size boost
  (`risk.bottoming_tail_size_multiplier`) -- same discrete
  threshold-and-multiplier treatment as every other soft signal
  (catalyst, obviousness, shallow pullback), exactly as that earlier
  deferral note anticipated ("would follow the same soft-boost pattern...
  if added").
- **`is_momentum_exhausted()`** (`indicators.py`) -- sequential shrinking
  green-candle bodies *and* shrinking volume across N bars (default 3),
  a trend-exhaustion warning distinct from any single bearish-shaped
  candle. Both conditions are required jointly, matching the source
  material's explicit nuance that shrinking body size alone (with volume
  still rising) is a weaker, lower-confidence version of this signal, not
  the same thing. Wired into `PositionManager._check_reversal_exit` as a
  fifth exit reason (`momentum_exhaustion`), alongside the existing
  topping-tail/red-after-green/first-lower-low/volume-burst checks --
  the natural home for it, since it's the same "OHLCV-computable
  exit warning" bucket those already live in. Unlike "first lower low,"
  it isn't gated behind breakeven, since it's a distinct signal that
  doesn't need the position already profitable to be meaningful.

**Confirmed already correct, no change needed:**
- **"Candle over candle" / "candle under candle."** `is_lower_low()`
  already *is* "candle under candle" (already used exactly as this
  file's confirmation-after-a-warning-candle pattern:
  `PositionManager`'s reversal exit fires "first lower low" as
  confirmation after a topping-tail/red-after-green warning). No
  `is_higher_high()` counterpart was added: the bullish breakout entries
  across all three breakout-style strategies already require the close
  to clear a *multi-bar computed range high* (`flag_high`, `b_high`,
  `breakout_high`), which is a stronger version of "candle over candle"
  (a single immediately-prior bar's high) rather than a gap needing a
  separate, weaker primitive alongside it. Adding one with no real
  caller would have been unused code.
- **Data hierarchy (Level 2 -> candlesticks -> indicators) and sub-bar/
  tick-level entry triggers.** This is the clearest statement yet of why
  the bot's 1-minute-bar-only architecture is structurally slower than
  the trader it's modeling, but it doesn't add anything actionable beyond
  the existing deferrals: `broker/market_data.py::subscribe_real_time_bars`
  (5-second bars) is still dead code (see "Deferred: ~30-second
  post-entry 'instant resolution' check" below), and Level 2/order-book
  data is still the same deferred gap noted under "sector heat and Level
  2" below. No new deferral needed -- this file is additional motivation
  for the existing ones, not a new one.
- **Context-dependency (candlestick signals matter more after a clear
  trend, less during sideways/choppy action).** Already structurally true
  of this bot: `pullback_validity.py`'s candle-shape checks only ever run
  *after* a strategy has already confirmed a qualifying directional
  move (`min_spike_pct`, `min_ab_move_pct`, `min_gap_pct`) -- there's no
  code path where a candle-shape gate gets evaluated during undifferentiated
  sideways action in the first place, so no additional "is this a trend"
  classifier was needed.
- **Doji variant taxonomy** (standard/long-legged/gravestone/dragonfly).
  These are graduated points along the same continuous wick-ratio/body
  spectrum `candle_strength()` and the topping/bottoming-tail wick-ratio
  checks already measure -- a "gravestone doji" is just a near-zero-body
  topping tail, a "dragonfly doji" a near-zero-body bottoming tail. No
  discrete named classifiers add detection capability beyond what's
  already parametrically expressible; not built out as a separate
  taxonomy.

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
  bot's current configured scale -- `risk.max_position_pct_of_buying_power`
  (0.25) already keeps position size proportional to the account and well
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
  capped by `risk.max_position_pct_of_buying_power` (a ceiling relative to
  *current* buying power, not a fixed dollar/share count -- replaced the
  old fixed `max_position_notional_usd`/`max_shares_per_trade` pair so the
  cap scales correctly across account sizes, from a ~$1,000 live account
  up) -- never as a fixed fraction of equity tied to risk-per-trade. No tension
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

The dip-buying transcript gives (1) a much more specific version, worth
recording here rather than re-deferring blind if revisited: a **graduated**
response scale rather than a single 50% trigger -- giving back 10% of
peak daily profit as a soft caution point (reinstate a reduced-size cap
for the rest of the session, conceptually the same lever as the existing
`risk.starter_trade_size_multiplier`/downgrade mechanism, just keyed off
peak-giveback instead of the first trade's outcome), 15-20% as a stronger
caution point (voluntary stop or further size cut), and 50% as the
existing hard mandatory halt. Also gives a concrete starter-size-cap
number independent of the giveback rule itself: cap new positions at
roughly 10-20% of full size until the day is up some threshold (the
transcript's own example: 5,000 shares of a 30-50k max, unlocking after
$1,000 of realized profit) -- **this specific piece is already fully
supported by the existing `risk.daily_profit_goal_usd` +
`risk.cushion_profit_fraction`/`cushion_size_fraction` mechanism**, just
not enabled by default (`daily_profit_goal_usd: null`) since a meaningful
dollar unlock threshold is inherently account-size-specific and isn't
something to default to an invented number for someone else's account.
Still deferred: the maintainer's explicit request to hold off stands: this
is additional detail for the *next* time this circuit breaker is revisited
deliberately, not a signal to implement it now off the back of a second
transcript restating the same idea with more precision.

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

The leverage-mechanics transcript's "stacked sellers" (many moderate
resting sell orders spread across several price levels near a resistance
point, functionally the same overhead-supply signal as one large order,
just distributed) is the same gap, not a new one — still needs
market-depth data this bot doesn't subscribe to. If/when L2 support is
ever added, worth summing resting ask-side size across a price band near
a target level (not just checking best-ask or a single abnormal-size
order) so this shape is covered by the same feature as the single-large-
seller case, rather than needing two separate detectors.

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

The leverage-mechanics transcript names this same principle "resulting"
(Annie Duke's term, from *Thinking in Bets*, for judging a decision by its
outcome rather than the quality of the reasoning behind it at the time it
was made) — worth keeping as the searchable term if this is ever built
out, since it's a precise match for what a rule-adherence flag would
actually be grading against. Doesn't change the deferral: this bot's
signals are already 100% rule-adherent by construction (see "Confirmed
already correct" under "Implemented: 'TA Master Class' additions" above,
which is the distinct-but-related "is the rule set itself well
calibrated" question), so a "resulting"-aware tag would only ever have
something to flag for a human reviewing *why* a rule fired the way it did
— not for catching an automated rule violation, since none can occur.

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
