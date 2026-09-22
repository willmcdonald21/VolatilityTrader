from __future__ import annotations

from datetime import time
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, model_validator

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class TradingConfig(BaseModel):
    mode: str = "paper"
    host: str = "127.0.0.1"
    port: int = 4002
    client_id: int = 7
    i_understand_live_trading: bool = False
    use_rth: bool = False

    @model_validator(mode="after")
    def _guard_live_trading(self) -> "TradingConfig":
        if self.mode not in ("paper", "live"):
            raise ValueError(f"trading.mode must be 'paper' or 'live', got {self.mode!r}")
        if self.mode == "live" and not self.i_understand_live_trading:
            raise ValueError(
                "trading.mode is 'live' but i_understand_live_trading is not true. "
                "This bot defaults to paper-only; flipping to live requires an explicit opt-in."
            )
        if self.mode == "paper" and self.port in (4001, 7496):
            raise ValueError(
                f"trading.mode is 'paper' but port {self.port} is a live-account port. "
                "Use 4002 (Gateway paper) or 7497 (TWS paper)."
            )
        return self


class ExecutionConfig(BaseModel):
    # IBKR rejects plain market orders outside regular trading hours
    # (4:00-9:30am / 4:00-8:00pm ET) -- a plain StopOrder resolves to a
    # market fill once triggered, so the stop-loss leg is built as a
    # stop-limit (STP LMT) instead. This offset sits the limit price this
    # % beyond the stop trigger, capping worst-case slippage the same way
    # the source material's "ask+10c/bid-10c" marketable-limit pattern does.
    stop_limit_offset_pct: float = Field(default=0.5, ge=0)
    # Emergency flattens outside regular hours use a marketable LIMIT this %
    # through the last price, because IBKR ignores outsideRth on market
    # orders and simply queues them until 09:30 (2026-09-21: the daily-loss
    # flatten at 04:18 ET didn't execute until the open).
    flatten_limit_offset_pct: float = Field(default=5.0, gt=0)


class RiskConfig(BaseModel):
    daily_loss_limit_pct: float = Field(gt=0, le=0.5)
    flatten_on_daily_loss_limit: bool = False
    max_concurrent_positions: int = Field(gt=0)
    # A slice of max_concurrent_positions held back exclusively for
    # scanner_rank <= reserved_top_tier_max_rank, usable only as overflow
    # once every unrestricted slot (max_concurrent_positions -
    # reserved_top_tier_slots) is already occupied -- so a best-of-day
    # candidate still has a shot even when the book is already full of
    # average names, rather than being turned away outright. Default 0
    # keeps existing configs/tests behaving exactly as before (no reserve).
    reserved_top_tier_slots: int = Field(default=0, ge=0)
    reserved_top_tier_max_rank: int = Field(default=3, ge=1)
    # Single-trade ceiling expressed relative to *current* buying power,
    # not a fixed dollar or share count -- a fixed number is meaningless
    # across account sizes (2,000 shares is nothing on a $1M account and
    # more than the whole account on a $1,000 one). Paired with
    # max_concurrent_positions above: at the default 0.25 and 3 positions,
    # at most ~75% of buying power is deployable at once, leaving headroom.
    max_position_pct_of_buying_power: float = Field(gt=0, le=1.0)
    daily_profit_goal_usd: float | None = None
    cushion_profit_fraction: float = Field(default=0.25, gt=0, le=1)
    cushion_size_fraction: float = Field(default=0.25, gt=0, le=1)
    # First entry into a symbol: this fraction of AvailableFunds. A second
    # signal on a symbol already holding one lot may add on at
    # addon_pct_of_funds; a third signal on the same symbol is rejected
    # (RiskManager._size_position enforces the 2-lot cap via
    # PositionManager.open_lot_count).
    first_entry_pct_of_funds: float = Field(default=0.10, gt=0, le=1.0)
    addon_pct_of_funds: float = Field(default=0.05, gt=0, le=1.0)
    # Fraction of start-of-day equity risked per trade, i.e. what is lost if
    # the stop fills. When set, this becomes the PRIMARY sizing rule and
    # share count falls out of the stop distance (budget / risk-per-share)
    # instead of being a flat slice of the account -- so a wider, structurally
    # correct stop costs fewer shares rather than more dollars, and every
    # trade risks the same amount whatever its stop distance. The
    # %-of-funds numbers above stay on as notional caps.
    #
    # Sizing off notional alone is what forced max_stop_distance_pct down to
    # a level that cannot work on this bot's universe: with share count
    # independent of stop distance, the only way to bound per-trade loss was
    # to bound the stop itself, which put every stop ~1.7% from entry --
    # inside the normal 1-minute noise of a $1-3 low-float stock. On both
    # 2026-09-15 and 2026-09-16 that produced a 100% stop-out rate with not
    # one profit tier ever filled.
    #
    # null disables it and restores pure notional sizing.
    risk_per_trade_pct: float | None = Field(default=None, gt=0, le=0.1)
    addon_risk_pct: float | None = Field(default=None, gt=0, le=0.1)

    @model_validator(mode="after")
    def _guard_risk_sizing(self) -> "RiskConfig":
        if self.addon_risk_pct is not None and self.risk_per_trade_pct is None:
            raise ValueError(
                "risk.addon_risk_pct is set but risk.risk_per_trade_pct is not -- "
                "risk-based sizing is either on for both lots or off for both."
            )
        return self
    # Global conservative stop-loss cap, applied uniformly in
    # WarriorBot._handle_signal regardless of which strategy produced the
    # signal -- tightens (never loosens) each strategy's own structural
    # stop if that stop would risk more than this % of entry price.
    max_stop_distance_pct: float = Field(default=2.0, gt=0)
    # How long a symbol stays ineligible after one of ITS SIGNALS WAS
    # REJECTED (never after an accepted one -- an entry still permanently
    # uses up that strategy's one shot at the symbol for the day). Every
    # rejection reason is transient capacity, so the setup deserves another
    # look once the book frees up; the delay just stops a level-triggered
    # setup from re-firing on every single bar in the meantime.
    rejected_signal_cooldown_seconds: float = Field(default=300.0, gt=0)
    # A working entry (parent) order left unfilled this long is cancelled.
    # These are DAY limit orders at the signal bar's close, so without this
    # they rest until 15:55 and can fill hours after the setup that
    # justified them is dead -- confirmed live: RLGT's 2026-09-15 04:30 ET
    # vwap_reversion entry filled at 08:26 ET, 3h56m later, still carrying
    # the stop computed from the 04:30 structure. Shares already filled keep
    # their stop; only the still-working remainder is cancelled.
    entry_fill_timeout_seconds: float = Field(default=300.0, gt=0)
    # No new entries before this ET time. 2026-09-21: 11 signals fired within
    # one second of the 04:00 ET open, on a first bar with almost no volume
    # or VWAP history behind it, and 7 symbols were bought at the opening
    # prints; the loss limit was breached within 18 minutes.
    no_entry_before_et: time = time(4, 15)
    # A second lot (the pyramid add-on) is only allowed this long after the
    # symbol's first lot was submitted. An add-on is supposed to confirm
    # strength; two strategies firing in the same second confirms nothing.
    addon_min_seconds_after_first_entry: float = Field(default=120.0, ge=0)
    # Count open positions' unrealized LOSS toward the daily loss limit, not
    # just realized P&L. Realized-only let three positions sit unprotected
    # and keep bleeding well past the limit on 2026-09-21.
    count_unrealized_loss_in_daily_limit: bool = True
    # A symbol already held by a DIFFERENT strategy is, by default, off
    # limits to a new signal -- two independent strategies firing on the
    # same name within minutes builds two uncoordinated brackets (separate
    # entry/stop/target, doubled real exposure, neither aware of the
    # other). Confirmed across the journal: this pattern wins 10% of the
    # time; 2026-09-22 alone had 7 instances totaling -$578.62, over half
    # that day's loss. A same-strategy add-on (the intentional pyramid,
    # still gated by addon_min_seconds_after_first_entry below) is
    # unaffected either way.
    allow_cross_strategy_stacking: bool = False

    @model_validator(mode="after")
    def _guard_reserved_slots(self) -> "RiskConfig":
        if self.reserved_top_tier_slots > self.max_concurrent_positions:
            raise ValueError(
                f"risk.reserved_top_tier_slots ({self.reserved_top_tier_slots}) cannot exceed "
                f"risk.max_concurrent_positions ({self.max_concurrent_positions})"
            )
        return self


class GapAndGoConfig(BaseModel):
    enabled: bool = True
    min_gap_pct: float = 10.0
    min_price: float = 1.0
    max_price: float = 20.0
    min_rel_volume: float = 5.0
    breakout_lookback_bars: int = 30
    stop_buffer_pct: float = 1.0
    target_r_multiple: float = 2.0
    enable_float_filter: bool = True
    max_float_shares: float = 10_000_000
    min_float_rotation: float = 0.0  # today's cumulative volume / float; 0 = disabled (needs float_list.csv data)
    min_breakout_candle_strength: float = Field(default=0.0, ge=-1.0, le=1.0)


class PullbackQualityConfig(BaseModel):
    """Shared entry-time dump-avoidance gates for bull_flag/abcd, on top of
    the existing volume/VWAP/9EMA/MACD checks in pullback_validity.py --
    "dip or dump" checklist items directly OHLCV-computable at entry time,
    not just as an exit signal (is_topping_tail/is_high_volume_red_bar
    already existed for PositionManager's reversal exit)."""

    reject_topping_tail: bool = True
    topping_tail_wick_ratio: float = Field(default=2.0, gt=0)
    reject_high_volume_red_bar: bool = True
    high_volume_red_bar_multiple: float = Field(default=2.0, gt=0)
    # Multi-timeframe confirmation: the same MACD/topping-tail checks,
    # recomputed on 5-minute bars resampled from the existing 1-minute
    # history (no new IB subscription needed) -- catches a deteriorating
    # 5-minute trend that a clean 1-minute pullback can mask.
    require_5m_macd_confirmation: bool = True
    reject_5m_topping_tail: bool = True
    # Precise pairwise volume-profile rule: each pullback bar's volume must
    # be lighter than the specific green candle immediately preceding the
    # pullback (up_move_bars[-1]) -- a stricter, more targeted check than
    # the aggregate "pullback total < up-move total" comparison above,
    # which can still pass while one individual red bar in a multi-bar
    # pullback outweighs the anchor green candle alone.
    require_pullback_lighter_than_prior_green_bar: bool = True
    # "Price and volume should be positively correlated as the stock
    # climbs" -- a stock advancing on declining volume is a divergence
    # warning even though price is still rising. Distinct from the volume
    # checks above, which compare the pullback against the up-move; this
    # checks whether the up-move itself was volume-confirmed.
    require_rising_volume_on_advance: bool = True


class BullFlagConfig(BaseModel):
    enabled: bool = True
    min_spike_pct: float = 5.0
    max_pullback_pct: float = 50.0
    min_consolidation_bars: int = 1  # "1 or more red candles" per source material -- a single-bar micro pullback is the ideal case, not an edge case
    max_consolidation_bars: int = 15
    stop_buffer_pct: float = 0.5
    target_r_multiple: float = 2.0
    min_rel_volume: float = 5.0  # Ross's stated hard floor -- "if it doesn't have at least 5x average volume, it's not worth touching"
    min_breakout_candle_strength: float = Field(default=0.0, ge=-1.0, le=1.0)


class AbcdConfig(BaseModel):
    enabled: bool = True
    min_ab_move_pct: float = 5.0
    min_bc_pullback_pct: float = 20.0
    max_bc_pullback_pct: float = 60.0
    stop_buffer_pct: float = 0.5
    target_r_multiple: float = 2.0
    min_rel_volume: float = 5.0  # same hard floor as the other strategies -- a blanket five-pillars criterion, not gap_and_go-specific
    min_breakout_candle_strength: float = Field(default=0.0, ge=-1.0, le=1.0)


class VwapReversionConfig(BaseModel):
    enabled: bool = True
    max_vwap_distance_pct: float = 1.5
    red_to_green_volume_multiple: float = 5.0  # was 2.0 -- raised to match Ross's stated 5x hard floor
    min_rel_volume: float = 5.0  # applies to the vwap_bounce setup, which previously had no relative-volume gate at all
    stop_buffer_pct: float = 0.5
    target_r_multiple: float = 1.5


class InvertedHeadAndShouldersConfig(BaseModel):
    # Secondary/non-primary pattern in the source material (a live example
    # from a trade recap, not one of the four core taught setups) -- off by
    # default until validated against real sessions, unlike the four
    # strategies above which are the source material's core taught setups.
    enabled: bool = False
    min_rel_volume: float = 5.0  # same blanket five-pillars floor as the other strategies
    # How far apart the two shoulder lows may sit, relative to the head low,
    # before the shape stops reading as a head-and-shoulders (shoulders
    # roughly comparable depth, not required to match exactly).
    max_shoulder_asymmetry_pct: float = Field(default=40.0, gt=0)
    # Head must dip meaningfully below the neckline -- guards against
    # noise-level 1-minute-bar wiggles registering as a "head."
    min_head_depth_pct: float = Field(default=3.0, gt=0)
    stop_buffer_pct: float = 0.5
    target_r_multiple: float = 2.0
    min_breakout_candle_strength: float = Field(default=0.0, ge=-1.0, le=1.0)


class StrategiesConfig(BaseModel):
    gap_and_go: GapAndGoConfig = GapAndGoConfig()
    bull_flag: BullFlagConfig = BullFlagConfig()
    abcd: AbcdConfig = AbcdConfig()
    vwap_reversion: VwapReversionConfig = VwapReversionConfig()
    inverted_head_and_shoulders: InvertedHeadAndShouldersConfig = InvertedHeadAndShouldersConfig()


class ProfitTierConfig(BaseModel):
    r_multiple: float = Field(gt=0)  # R-multiple at which this tier's limit order sits
    pct: float = Field(gt=0, lt=1)  # fraction of the *original* position size closed at this tier


class BreakevenConfig(BaseModel):
    enabled: bool = False
    trigger_r_multiple: float = Field(default=1.0, gt=0)


class TrailingConfig(BaseModel):
    enabled: bool = False
    method: Literal["ema", "atr"] = "atr"
    atr_period: int = Field(default=14, gt=1)
    atr_multiple: float = Field(default=1.5, gt=0)


class ReversalExitConfig(BaseModel):
    enabled: bool = False
    topping_tail_wick_ratio: float = Field(default=2.0, gt=0)  # upper wick >= this multiple of the candle body
    volume_burst_multiple: float = Field(default=2.0, gt=0)  # red-bar volume >= this multiple of recent avg volume
    volume_lookback_bars: int = Field(default=10, gt=0)
    momentum_exhaustion_lookback_bars: int = Field(default=3, gt=1)  # consecutive shrinking-body+shrinking-volume green bars


class ExitsConfig(BaseModel):
    # Ordered list of partial take-profit legs, each closing `pct` of the
    # *original* position size once price reaches `r_multiple`. Whatever
    # fraction remains after all tiers (1 - sum(pct)) rides on the
    # breakeven/trailing-stop logic below rather than a fixed final target.
    profit_tiers: list[ProfitTierConfig] = [
        ProfitTierConfig(r_multiple=1.0, pct=0.34),
        ProfitTierConfig(r_multiple=2.0, pct=0.33),
    ]
    breakeven: BreakevenConfig = BreakevenConfig()
    trailing: TrailingConfig = TrailingConfig()
    reversal_exit: ReversalExitConfig = ReversalExitConfig()
    eod_flatten_time: time = time(15, 55)  # 5 min before RTH_CLOSE; force-flatten at/after this ET time
    risk_loop_interval_seconds: int = Field(default=15, gt=0)

    @model_validator(mode="after")
    def _guard_profit_tiers(self) -> "ExitsConfig":
        total_pct = sum(tier.pct for tier in self.profit_tiers)
        if total_pct >= 1.0:
            raise ValueError(
                f"exits.profit_tiers percentages sum to {total_pct}, must be < 1.0 "
                "so some quantity remains for the stop/trailing logic to manage"
            )
        r_multiples = [tier.r_multiple for tier in self.profit_tiers]
        if r_multiples != sorted(r_multiples):
            raise ValueError("exits.profit_tiers must be ordered by ascending r_multiple")
        return self


class NewsConfig(BaseModel):
    enabled: bool = False
    lookback_hours: int = Field(default=48, gt=0)
    provider_codes: str = ""  # empty = auto-discover entitled providers via reqNewsProviders() at startup


class ScannerConfig(BaseModel):
    scan_code: str = "TOP_PERC_GAIN"
    location_code: str = "STK.US.MAJOR"
    above_price: float = 1.0
    below_price: float = 20.0
    # IBKR's scanner filters on raw share volume, not relative-to-average --
    # it can't compute true relative volume itself (needs each symbol's own
    # average, which isn't known until after onboarding). This is just a
    # coarse liquidity sanity floor to keep dead tickers out of the
    # candidate list; the REAL 5x relative-volume gate (Ross's stated hard
    # minimum) is enforced per-strategy after onboarding via
    # SymbolContext.relative_volume() + each strategy's min_rel_volume.
    # Keep this low -- a low-float stock running 5x its own (small) average
    # volume can still have modest raw share volume.
    above_volume: int = 10_000
    refresh_seconds: int = 60
    max_candidates: int = 25


class DataWatchdogConfig(BaseModel):
    """Guards against the failure mode confirmed live on 2026-09-15: RETO
    (that day's #1 scanner-ranked gainer) and several other symbols
    (WAFU/WNW/FTFT/ARTL/BGMS/SKIL) each got one onboarding log line, then
    never produced another strategy evaluation for the rest of the session
    -- real, actively-moving stocks the bot was simply blind to. Root cause
    had two independent faces, both stemming from the same gap: onboarded
    symbols were never unsubscribed or health-checked, so the bot's live
    subscription count only ever grew (by 11am that day it had already
    onboarded ~156-170 symbols). (1) new keepUpToDate requests made once
    IBKR's live-line budget is exhausted can fail outright, or nominally
    "succeed" (return the historical warmup) while the ongoing live stream
    never actually starts. (2) confirmed the same day: a transient
    IBKR-side "HMDS server disconnect" silently killed ~179 concurrent live
    subscriptions in a single burst (error 10182, 15:52 ET) with zero
    self-healing. This config drives two independent defenses: proactively
    freeing subscription budget before the cap is ever hit, and a
    staleness watchdog that detects and resubscribes any symbol whose feed
    has gone silent, regardless of cause.
    """

    enabled: bool = True
    check_interval_seconds: int = Field(default=60, gt=0)
    # No live update received in this long (during the bot's active
    # session) -> treat the subscription as dead and resubscribe. Comfortably
    # above the ~60s cadence of 1-minute bars, tight enough to catch a real
    # failure within a few minutes rather than losing the rest of the day to it.
    stale_after_seconds: int = Field(default=180, gt=0)
    # Self-imposed ceiling kept well below where things broke live
    # (~156-170 concurrent subscriptions) so the bot manages its own budget
    # instead of silently discovering IBKR's real, undocumented cap.
    max_concurrent_subscriptions: int = Field(default=90, gt=0)
    # A tracked symbol with no open position that hasn't appeared in the
    # scanner's top-N results for this long is eligible to be evicted (its
    # live subscription freed) to make room for a fresh, currently-relevant
    # candidate once at the cap above.
    inactive_unsubscribe_seconds: int = Field(default=1800, gt=0)


class PositionReconciliationConfig(BaseModel):
    """Guards against the failure mode confirmed live on 2026-09-16: NRXS
    ended up as an unprotected naked short of 850 shares for ~3h53m,
    closed only by luck -- the scheduled EOD flatten happened to still be
    ahead of it. Root cause: an IBKR disconnect/reconnect (ib_async's
    IB.disconnect() calls wrapper.reset(), which wipes its internal
    trades/permId2Trade dicts) silently orphaned PositionManager's fill
    listeners for orders that kept working at the broker across the
    reconnect. Two of NRXS's stop-loss exits filled correctly but
    invisibly to the bot, which never decremented its local qty or
    untracked the lot -- so a later stop-resize placed a brand-new,
    full-size duplicate stop on an already-flat lot, which itself later
    filled for real, doubling the exit into a naked short.

    resync_after_reconnect (position_manager.py) re-wires whatever
    listeners it can find fresh Trade objects for, but can't always
    resolve every case cleanly (e.g. an order that filled or was
    cancelled entirely while disconnected). This watchdog is the
    unconditional backstop: independent of *why* a symbol ended up here,
    it periodically checks IBKR's own live position/order state directly
    and immediately flattens (not re-protects -- simplest, always-correct,
    matches this bot's existing EOD-flatten/kill-switch philosophy of
    "get flat now" over reconstructing a stop at the "right" price) any
    symbol holding a real position with no resting protective stop
    order."""

    enabled: bool = True
    check_interval_seconds: int = Field(default=30, gt=0)


class JournalConfig(BaseModel):
    db_path: str = "data/journal.sqlite3"


class KillSwitchConfig(BaseModel):
    flag_file: str = "data/KILL_SWITCH"


class LoggingConfig(BaseModel):
    level: str = "INFO"
    file: str = "data/warrior_bot.log"


class NotificationsConfig(BaseModel):
    # Posts to five separate Discord channels, each its own webhook URL
    # read from an env var (see .env.example) -- never stored here, since
    # config.yaml is tracked in git.
    enabled: bool = False
    notify_on_signal: bool = True  # -> trade_activity: an entry signal was accepted and sized
    notify_on_fill: bool = True  # -> trade_activity: every buy/sell/trim fill, raw/unbatched
    # -> trade_activity_summary: one curated embed per completed entry (or
    # pyramid add-on) fill burst, debounced/coalesced across that burst's
    # individual partial fills -- see OrderManager._send_entry_summary.
    # Independent of notify_on_fill: the two channels serve different
    # purposes (raw audit trail vs. a clean per-entry summary) and are
    # each toggleable on their own.
    notify_on_entry_summary: bool = True
    notify_on_kill_switch: bool = True  # -> kill_switch: manual kill switch, IBKR connection loss
    notify_on_limits: bool = True  # -> limits: daily-loss-limit halt, EOD flatten, IBKR session failure
    notify_on_pnl: bool = True  # -> pnl: per-trade and running daily realized P&L on every closing/trim fill


class AppConfig(BaseModel):
    trading: TradingConfig
    execution: ExecutionConfig = ExecutionConfig()
    risk: RiskConfig
    strategies: StrategiesConfig
    pullback_quality: PullbackQualityConfig = PullbackQualityConfig()
    exits: ExitsConfig = ExitsConfig()
    news: NewsConfig = NewsConfig()
    notifications: NotificationsConfig = NotificationsConfig()
    scanner: ScannerConfig
    data_watchdog: DataWatchdogConfig = DataWatchdogConfig()
    position_reconciliation: PositionReconciliationConfig = PositionReconciliationConfig()
    journal: JournalConfig
    kill_switch: KillSwitchConfig
    logging: LoggingConfig

    def resolve_path(self, relative: str) -> Path:
        return PROJECT_ROOT / relative


def load_config(path: str | Path | None = None) -> AppConfig:
    load_dotenv(PROJECT_ROOT / ".env")
    config_path = Path(path) if path else PROJECT_ROOT / "config" / "config.yaml"
    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return AppConfig.model_validate(raw)
