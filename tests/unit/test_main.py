from __future__ import annotations

from datetime import datetime, timezone

from warrior_bot.config import (
    AppConfig,
    ExitsConfig,
    JournalConfig,
    KillSwitchConfig,
    LoggingConfig,
    NotificationsConfig,
    RiskConfig,
    ScannerConfig,
    StrategiesConfig,
    TradingConfig,
)
from warrior_bot.main import WarriorBot
from warrior_bot.signals.signal import Signal


def make_config(tmp_path) -> AppConfig:
    return AppConfig(
        trading=TradingConfig(),
        risk=RiskConfig(
            daily_loss_limit_pct=0.02,
            max_concurrent_positions=3,
            max_position_pct_of_buying_power=0.25,
        ),
        strategies=StrategiesConfig(),
        exits=ExitsConfig(),
        notifications=NotificationsConfig(),
        scanner=ScannerConfig(),
        journal=JournalConfig(db_path=str(tmp_path / "journal.sqlite3")),
        kill_switch=KillSwitchConfig(flag_file=str(tmp_path / "KILL_SWITCH")),
        logging=LoggingConfig(file=str(tmp_path / "warrior_bot.log")),
    )


def test_on_connected_is_noop_on_first_connect(tmp_path):
    bot = WarriorBot(make_config(tmp_path))

    # first connect: nothing tracked yet, nothing to drop
    bot._on_connected()

    assert bot.contexts == {}
    assert bot.contracts == {}
    assert bot._subscriptions == {}


def test_on_connected_drops_tracked_symbols_after_reconnect(tmp_path):
    bot = WarriorBot(make_config(tmp_path))
    bot.contexts["AAPL"] = object()
    bot.contracts["AAPL"] = object()
    bot._subscriptions["AAPL"] = object()

    bot._on_connected()

    # dropped, not re-subscribed here -- _scan_loop treats AAPL as new again
    # on its next iteration and calls _onboard_symbol for it
    assert bot.contexts == {}
    assert bot.contracts == {}
    assert bot._subscriptions == {}


def test_on_connected_does_not_touch_position_manager(tmp_path):
    bot = WarriorBot(make_config(tmp_path))
    bot.contexts["AAPL"] = object()
    bot.position_manager._positions["AAPL"] = object()

    bot._on_connected()

    # standing bracket orders are IBKR's problem to keep working, not ours
    # to re-establish -- only the scanning/bar-subscription side is reset
    assert "AAPL" in bot.position_manager._positions


def make_signal(entry=10.0, stop=9.0) -> Signal:
    return Signal(
        symbol="AAPL",
        strategy="gap_and_go",
        side="BUY",
        entry_price=entry,
        stop_price=stop,
        target_price=12.0,
        ts=datetime.now(timezone.utc),
    )


def test_clamp_stop_tightens_stop_wider_than_conservative_max(tmp_path):
    bot = WarriorBot(make_config(tmp_path))  # default max_stop_distance_pct=2.0
    signal = make_signal(entry=10.0, stop=9.0)  # risk=1.0 -> 10% of entry, wider than the 2% cap

    bot._clamp_stop_to_conservative_max(signal)

    assert signal.stop_price == 10.0 - (10.0 * 0.02)  # 9.8


def test_clamp_stop_leaves_already_tight_stop_untouched(tmp_path):
    bot = WarriorBot(make_config(tmp_path))
    signal = make_signal(entry=10.0, stop=9.9)  # risk=0.1 -> 1% of entry, already under the 2% cap

    bot._clamp_stop_to_conservative_max(signal)

    assert signal.stop_price == 9.9
