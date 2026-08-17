from __future__ import annotations

from warrior_bot.config import (
    AppConfig,
    JournalConfig,
    KillSwitchConfig,
    LoggingConfig,
    NotificationsConfig,
    RiskConfig,
    ScannerConfig,
    StrategiesConfig,
    TradingConfig,
)
from warrior_bot import logging_setup
from warrior_bot.logging_setup import alert, setup_logging


def make_config(tmp_path, **notification_overrides) -> AppConfig:
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
        scanner=ScannerConfig(),
        journal=JournalConfig(),
        kill_switch=KillSwitchConfig(),
        logging=LoggingConfig(file=str(tmp_path / "test.log")),
        notifications=NotificationsConfig(**notification_overrides),
    )


def _capture_sends(monkeypatch):
    calls = []
    monkeypatch.setattr(
        logging_setup, "send_discord_message", lambda content, channel: calls.append((content, channel))
    )
    return calls


def test_alert_with_no_channel_never_sends_to_discord(tmp_path, monkeypatch):
    setup_logging(make_config(tmp_path, enabled=True))
    calls = _capture_sends(monkeypatch)

    alert("routine rejection")

    assert calls == []


def test_alert_kill_switch_channel_sent_when_enabled(tmp_path, monkeypatch):
    setup_logging(make_config(tmp_path, enabled=True, notify_on_kill_switch=True))
    calls = _capture_sends(monkeypatch)

    alert("kill switch active", channel="kill_switch")

    assert len(calls) == 1
    assert calls[0][1] == "kill_switch"
    assert "kill switch active" in calls[0][0]


def test_alert_limits_channel_sent_when_enabled(tmp_path, monkeypatch):
    setup_logging(make_config(tmp_path, enabled=True, notify_on_limits=True))
    calls = _capture_sends(monkeypatch)

    alert("daily loss limit breached", channel="limits")

    assert len(calls) == 1
    assert calls[0][1] == "limits"


def test_alert_not_sent_when_notifications_disabled(tmp_path, monkeypatch):
    setup_logging(make_config(tmp_path, enabled=False, notify_on_kill_switch=True))
    calls = _capture_sends(monkeypatch)

    alert("kill switch active", channel="kill_switch")

    assert calls == []


def test_alert_not_sent_when_that_channels_flag_disabled(tmp_path, monkeypatch):
    setup_logging(make_config(tmp_path, enabled=True, notify_on_kill_switch=False))
    calls = _capture_sends(monkeypatch)

    alert("kill switch active", channel="kill_switch")

    assert calls == []


def test_alert_channel_flags_are_independent(tmp_path, monkeypatch):
    setup_logging(make_config(tmp_path, enabled=True, notify_on_kill_switch=True, notify_on_limits=False))
    calls = _capture_sends(monkeypatch)

    alert("kill switch active", channel="kill_switch")
    alert("daily loss limit breached", channel="limits")

    assert len(calls) == 1
    assert calls[0][1] == "kill_switch"
