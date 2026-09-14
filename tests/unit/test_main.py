from __future__ import annotations

from datetime import datetime, timedelta, timezone

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
from warrior_bot.risk.account_state import AccountSnapshot
from warrior_bot.signals.signal import Signal
from warrior_bot.utils.time_utils import to_eastern


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


def test_clamp_stop_result_is_tick_conformant(tmp_path):
    # entry_price * pct / 100.0 is raw float arithmetic -- entry=2.21,
    # max_stop_distance_pct=2.0 produces stop=2.1658 (4 decimals) unless
    # rounded, which IBKR rejects with error 110 (this is the exact
    # regression seen live against SCNI on 2026-09-14: signal.stop_price is
    # mutated here *after* Signal.__post_init__ already ran, so its own
    # rounding doesn't cover this assignment).
    bot = WarriorBot(make_config(tmp_path))
    signal = make_signal(entry=2.21, stop=2.05)  # risk=0.16 -> ~7.2% of entry, wider than the 2% cap

    bot._clamp_stop_to_conservative_max(signal)

    assert signal.stop_price == round(signal.stop_price, 2)


def _fake_snapshot(open_positions_count=0) -> AccountSnapshot:
    return AccountSnapshot(
        net_liquidation=10_000,
        available_funds=10_000,
        buying_power=10_000,
        open_positions_count=open_positions_count,
        daily_realized_pnl=0.0,
    )


def _today_et():
    return to_eastern(datetime.now(timezone.utc)).date()


def test_check_new_trading_day_noop_on_same_day(tmp_path):
    bot = WarriorBot(make_config(tmp_path))
    bot.account_state.snapshot = lambda: _fake_snapshot()
    bot._trading_day = _today_et()
    bot._flattened_today = True

    bot._check_new_trading_day()

    assert bot._flattened_today is True  # unchanged -- still the same trading day


def test_check_new_trading_day_resets_flattened_today_flag(tmp_path):
    bot = WarriorBot(make_config(tmp_path))
    bot.account_state.snapshot = lambda: _fake_snapshot()
    bot._trading_day = (datetime.now(timezone.utc) - timedelta(days=1)).date()
    bot._flattened_today = True  # simulates yesterday's EOD flatten having already fired

    bot._check_new_trading_day()

    assert bot._flattened_today is False  # today's own EOD flatten can fire again
    assert bot._trading_day == _today_et()


def test_check_new_trading_day_alerts_when_positions_still_open(tmp_path, monkeypatch):
    alerts = []
    monkeypatch.setattr("warrior_bot.main.alert", lambda message, channel=None: alerts.append((message, channel)))
    bot = WarriorBot(make_config(tmp_path))
    bot.account_state.snapshot = lambda: _fake_snapshot(open_positions_count=2)
    bot._trading_day = (datetime.now(timezone.utc) - timedelta(days=1)).date()

    bot._check_new_trading_day()

    assert len(alerts) == 1
    message, channel = alerts[0]
    assert "2 position" in message
    assert channel == "limits"


def test_check_new_trading_day_no_alert_when_no_positions_open(tmp_path, monkeypatch):
    alerts = []
    monkeypatch.setattr("warrior_bot.main.alert", lambda message, channel=None: alerts.append((message, channel)))
    bot = WarriorBot(make_config(tmp_path))
    bot.account_state.snapshot = lambda: _fake_snapshot(open_positions_count=0)
    bot._trading_day = (datetime.now(timezone.utc) - timedelta(days=1)).date()

    bot._check_new_trading_day()

    assert alerts == []
