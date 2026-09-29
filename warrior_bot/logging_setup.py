from __future__ import annotations

import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from warrior_bot.config import AppConfig, NotificationsConfig
from warrior_bot.notify.discord import send_discord_message

_alert_logger: logging.Logger | None = None
_notifications_config: NotificationsConfig | None = None


def setup_logging(config: AppConfig) -> logging.Logger:
    global _alert_logger, _notifications_config
    _notifications_config = config.notifications

    log_path = config.resolve_path(config.logging.file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("warrior_bot")
    logger.setLevel(config.logging.level)
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")

    # Rotating, not a plain FileHandler. At DEBUG this bot writes ~31-35 MB
    # per active trading day (305,500 lines on 2026-09-21); by 2026-09-28
    # the single log file had reached 418 MB / 3.6M lines with no bound at
    # all, on track for ~8.6 GB/year -- and it lives in the same directory
    # as journal.sqlite3, so filling the disk would take down trading and
    # record-keeping together. Rotating daily (rather than by size) also
    # makes per-day forensics a matter of opening one file.
    file_handler = TimedRotatingFileHandler(
        log_path,
        when="midnight",
        backupCount=config.logging.backup_count,
        encoding="utf-8",
        utc=False,
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(config.logging.console_level or config.logging.level)
    console_handler.setFormatter(fmt)
    logger.addHandler(console_handler)

    _alert_logger = logger
    return logger


_CHANNEL_EMOJI = {"kill_switch": "🚨", "limits": "⛔"}


def alert(message: str, channel: str | None = None) -> None:
    """Loud alert path for risk-manager rejections, kill-switch events, and
    daily limits -- always logs at WARNING. If `channel` is given
    ("kill_switch" or "limits") and notifications.enabled plus that
    channel's flag are both true, also pushes to that Discord channel.
    `channel=None` means "log only" -- used for routine rejections (max
    concurrent positions, size rounds to zero) that don't warrant a ping.
    """
    logger = _alert_logger or logging.getLogger("warrior_bot")
    logger.warning("ALERT: %s", message)
    if channel is None or not _notifications_config or not _notifications_config.enabled:
        return
    if channel == "kill_switch" and _notifications_config.notify_on_kill_switch:
        send_discord_message(f"{_CHANNEL_EMOJI['kill_switch']} {message}", channel="kill_switch")
    elif channel == "limits" and _notifications_config.notify_on_limits:
        send_discord_message(f"{_CHANNEL_EMOJI['limits']} {message}", channel="limits")
