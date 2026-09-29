from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from warrior_bot.backtest import replay as replay_module
from warrior_bot.backtest.replay import describe_limitations, replay_symbol
from warrior_bot.config import (
    AppConfig, DataWatchdogConfig, ExitsConfig, JournalConfig, KillSwitchConfig,
    LoggingConfig, NotificationsConfig, RiskConfig, ScannerConfig, StrategiesConfig, TradingConfig,
)
from warrior_bot.strategies.bull_flag import BullFlagStrategy
from warrior_bot.strategies.gap_and_go import GapAndGoStrategy

BASE = datetime(2026, 1, 5, 9, 30, tzinfo=timezone.utc)

# The same spike -> pullback -> breakout shape the strategy unit tests use,
# padded so it sits at the end of a realistic session.
_SETUP = [
    (10.0, 10.0, 9.9, 10.0, 1000),
    (10.0, 12.0, 10.0, 11.8, 3000),
    (11.8, 11.75, 11.6, 11.65, 300),
    (11.65, 11.7, 11.55, 11.6, 300),
    (11.6, 11.65, 11.5, 11.55, 300),
    (11.55, 11.95, 11.55, 11.95, 1000),
]


def _ib_bars(specs):
    return [
        SimpleNamespace(date=BASE + timedelta(minutes=i), open=o, high=h, low=lo, close=c, volume=v)
        for i, (o, h, lo, c, v) in enumerate(specs)
    ]


class FakeReplayIB:
    def __init__(self, minute_bars, daily_bars=None):
        self._minute_bars = minute_bars
        self._daily_bars = daily_bars if daily_bars is not None else [SimpleNamespace(volume=10_000)]

    async def reqHistoricalDataAsync(self, contract, **kwargs):
        return self._daily_bars if kwargs.get("barSizeSetting") == "1 day" else self._minute_bars


def _config(tmp_path):
    return AppConfig(
        trading=TradingConfig(),
        risk=RiskConfig(daily_loss_limit_pct=0.02, max_concurrent_positions=3, max_position_pct_of_buying_power=0.25),
        strategies=StrategiesConfig(),
        exits=ExitsConfig(),
        notifications=NotificationsConfig(),
        scanner=ScannerConfig(),
        data_watchdog=DataWatchdogConfig(),
        journal=JournalConfig(db_path=str(tmp_path / "j.sqlite3")),
        kill_switch=KillSwitchConfig(flag_file=str(tmp_path / "K")),
        logging=LoggingConfig(file=str(tmp_path / "l.log")),
    )


def _patch_prior_close(monkeypatch, value=10.5):
    async def _fake(ib, contract):
        return value

    monkeypatch.setattr(replay_module, "fetch_prior_close", _fake)


def test_replay_produces_at_least_one_signal_on_a_known_good_setup(tmp_path, monkeypatch):
    # THE regression guard. Until 2026-09-28 replay_symbol built a
    # SymbolContext with no prior_close and no avg_daily_volume, so every
    # strategy rejected at its first gate and this returned [] no matter
    # what the code under test did -- a pre-deploy check that could never
    # fail, and therefore never told you anything.
    _patch_prior_close(monkeypatch)
    ib = FakeReplayIB(_ib_bars(_SETUP))
    config = _config(tmp_path)
    strategies = [
        GapAndGoStrategy(config.strategies.gap_and_go, pullback_quality_config=config.pullback_quality),
        BullFlagStrategy(config.strategies.bull_flag, config.pullback_quality),
    ]

    signals = asyncio.run(replay_symbol(ib, SimpleNamespace(symbol="GOGO"), strategies, config))

    assert signals, "replay produced no signals on a setup the unit tests accept"
    assert {s["strategy"] for s in signals} <= {"gap_and_go", "bull_flag"}


def test_replay_populates_the_context_the_way_onboarding_does(tmp_path, monkeypatch):
    _patch_prior_close(monkeypatch, value=10.5)
    ib = FakeReplayIB(_ib_bars(_SETUP), daily_bars=[SimpleNamespace(volume=1000), SimpleNamespace(volume=3000)])
    config = _config(tmp_path)

    captured = {}

    class SpyStrategy:
        name = "spy"
        enabled = True

        def evaluate(self, ctx, now):
            captured["prior_close"] = ctx.prior_close
            captured["avg_daily_volume"] = ctx.avg_daily_volume
            captured["scanner_rank"] = ctx.scanner_rank
            return None

    asyncio.run(replay_symbol(ib, SimpleNamespace(symbol="GOGO"), [SpyStrategy()], config))

    assert captured["prior_close"] == 10.5
    assert captured["avg_daily_volume"] == 2000  # mean of the daily bars
    assert captured["scanner_rank"] == 1


def test_replay_refuses_to_run_blind_rather_than_reporting_nothing(tmp_path, monkeypatch):
    # Reporting "0 signals" when the context is unusable is exactly the
    # failure this module had. Fail loudly instead.
    async def _no_prior_close(ib, contract):
        return None

    monkeypatch.setattr(replay_module, "fetch_prior_close", _no_prior_close)
    ib = FakeReplayIB(_ib_bars(_SETUP))
    config = _config(tmp_path)

    with pytest.raises(ValueError, match="would reject at its"):
        asyncio.run(replay_symbol(ib, SimpleNamespace(symbol="GOGO"), [], config))


def test_limitations_are_stated_explicitly():
    text = describe_limitations()
    assert "adverse-selection" in text
    assert "reversal exits" in text
    assert "does NOT model" in text
