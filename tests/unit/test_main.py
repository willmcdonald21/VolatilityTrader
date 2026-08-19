from __future__ import annotations

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


def make_config(tmp_path) -> AppConfig:
    return AppConfig(
        trading=TradingConfig(),
        risk=RiskConfig(
            risk_per_trade_pct=0.01,
            daily_loss_limit_pct=0.02,
            max_concurrent_positions=3,
            max_position_notional_usd=5000,
            max_shares_per_trade=2000,
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
