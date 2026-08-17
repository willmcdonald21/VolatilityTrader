from __future__ import annotations

from datetime import time
from pathlib import Path
from typing import Literal

import yaml
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


class RiskConfig(BaseModel):
    risk_per_trade_pct: float = Field(gt=0, le=0.05)
    daily_loss_limit_pct: float = Field(gt=0, le=0.5)
    flatten_on_daily_loss_limit: bool = False
    max_concurrent_positions: int = Field(gt=0)
    max_position_notional_usd: float = Field(gt=0)
    max_shares_per_trade: int = Field(gt=0)
    daily_profit_goal_usd: float | None = None
    cushion_profit_fraction: float = Field(default=0.25, gt=0, le=1)
    cushion_size_fraction: float = Field(default=0.25, gt=0, le=1)
    catalyst_size_multiplier: float = Field(default=1.25, ge=1.0)
    time_of_day_boost_start: time = time(7, 0)
    time_of_day_boost_end: time = time(10, 0)
    time_of_day_size_multiplier: float = Field(default=1.25, ge=1.0)


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


class BullFlagConfig(BaseModel):
    enabled: bool = True
    min_spike_pct: float = 5.0
    max_pullback_pct: float = 50.0
    min_consolidation_bars: int = 1  # "1 or more red candles" per source material -- a single-bar micro pullback is the ideal case, not an edge case
    max_consolidation_bars: int = 15
    stop_buffer_pct: float = 0.5
    target_r_multiple: float = 2.0


class AbcdConfig(BaseModel):
    enabled: bool = True
    min_ab_move_pct: float = 5.0
    min_bc_pullback_pct: float = 20.0
    max_bc_pullback_pct: float = 60.0
    stop_buffer_pct: float = 0.5
    target_r_multiple: float = 2.0


class VwapReversionConfig(BaseModel):
    enabled: bool = True
    max_vwap_distance_pct: float = 1.5
    red_to_green_volume_multiple: float = 2.0
    stop_buffer_pct: float = 0.5
    target_r_multiple: float = 1.5


class StrategiesConfig(BaseModel):
    gap_and_go: GapAndGoConfig = GapAndGoConfig()
    bull_flag: BullFlagConfig = BullFlagConfig()
    abcd: AbcdConfig = AbcdConfig()
    vwap_reversion: VwapReversionConfig = VwapReversionConfig()


class ScaleOutConfig(BaseModel):
    enabled: bool = False
    pct: float = Field(default=0.5, gt=0, lt=1)  # fraction of the position closed at the scale-out price
    r_multiple: float = Field(default=1.0, gt=0)  # R-multiple at which the scale-out limit order sits


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


class ExitsConfig(BaseModel):
    scale_out: ScaleOutConfig = ScaleOutConfig()
    breakeven: BreakevenConfig = BreakevenConfig()
    trailing: TrailingConfig = TrailingConfig()
    reversal_exit: ReversalExitConfig = ReversalExitConfig()
    eod_flatten_time: time = time(15, 55)  # 5 min before RTH_CLOSE; force-flatten at/after this ET time
    risk_loop_interval_seconds: int = Field(default=15, gt=0)


class NewsConfig(BaseModel):
    enabled: bool = False
    lookback_hours: int = Field(default=48, gt=0)
    provider_codes: str = ""  # empty = auto-discover entitled providers via reqNewsProviders() at startup


class ScannerConfig(BaseModel):
    scan_code: str = "TOP_PERC_GAIN"
    location_code: str = "STK.US.MAJOR"
    above_price: float = 1.0
    below_price: float = 20.0
    above_volume: int = 100_000
    refresh_seconds: int = 60
    max_candidates: int = 25


class JournalConfig(BaseModel):
    db_path: str = "data/journal.sqlite3"


class KillSwitchConfig(BaseModel):
    flag_file: str = "data/KILL_SWITCH"


class LoggingConfig(BaseModel):
    level: str = "INFO"
    file: str = "data/warrior_bot.log"


class AppConfig(BaseModel):
    trading: TradingConfig
    execution: ExecutionConfig = ExecutionConfig()
    risk: RiskConfig
    strategies: StrategiesConfig
    exits: ExitsConfig = ExitsConfig()
    news: NewsConfig = NewsConfig()
    scanner: ScannerConfig
    journal: JournalConfig
    kill_switch: KillSwitchConfig
    logging: LoggingConfig

    def resolve_path(self, relative: str) -> Path:
        return PROJECT_ROOT / relative


def load_config(path: str | Path | None = None) -> AppConfig:
    config_path = Path(path) if path else PROJECT_ROOT / "config" / "config.yaml"
    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return AppConfig.model_validate(raw)
