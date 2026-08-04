from __future__ import annotations

import logging
from pathlib import Path

from warrior_bot.config import AppConfig

_alert_logger: logging.Logger | None = None


def setup_logging(config: AppConfig) -> logging.Logger:
    global _alert_logger

    log_path = config.resolve_path(config.logging.file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("warrior_bot")
    logger.setLevel(config.logging.level)
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(fmt)
    logger.addHandler(console_handler)

    _alert_logger = logger
    return logger


def alert(message: str) -> None:
    """Loud alert path for risk-manager rejections and kill-switch events.

    Currently logs at WARNING; this is the single hook to extend with a
    desktop/Slack/email notifier later without touching call sites.
    """
    logger = _alert_logger or logging.getLogger("warrior_bot")
    logger.warning("ALERT: %s", message)
