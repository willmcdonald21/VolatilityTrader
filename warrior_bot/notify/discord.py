from __future__ import annotations

import json
import logging
import os
import threading
import urllib.request

logger = logging.getLogger("warrior_bot.notify.discord")

_DISCORD_MESSAGE_LIMIT = 2000

# Four separate channels, each its own webhook -- kill-switch/connection
# events, daily-limit/EOD "trading stopped" events, routine trade activity
# (signals + fills), and per-trade/daily P&L each land in their own place
# rather than one noisy firehose.
_CHANNEL_ENV_VARS = {
    "kill_switch": "DISCORD_WEBHOOK_KILL_SWITCH",
    "limits": "DISCORD_WEBHOOK_LIMITS",
    "trade_activity": "DISCORD_WEBHOOK_TRADE_ACTIVITY",
    "pnl": "DISCORD_WEBHOOK_PNL",
}


def _post_payload(payload: dict, channel: str) -> None:
    """Fire-and-forget POST of a Discord webhook payload to one of the
    named channels (see _CHANNEL_ENV_VARS), whose URL is read from an
    environment variable -- deliberately never from config.yaml, which is
    tracked in git. No-ops silently if that channel's variable isn't set,
    so notifications are opt-in with zero setup cost otherwise.

    Runs the actual HTTP call on a background thread rather than making
    the caller `await` it: this needs to be safely callable from both the
    async main bot loop and synchronous contexts (scripts/kill_switch.py
    has no running event loop), and a slow/failed webhook call must never
    block or fail trading logic.
    """
    env_var = _CHANNEL_ENV_VARS.get(channel)
    if env_var is None:
        raise ValueError(f"Unknown notification channel {channel!r}, expected one of {list(_CHANNEL_ENV_VARS)}")

    webhook_url = os.environ.get(env_var)
    if not webhook_url:
        return

    def _post() -> None:
        try:
            data = json.dumps(payload).encode("utf-8")
            request = urllib.request.Request(
                webhook_url,
                data=data,
                # Discord's edge (Cloudflare) rejects requests with urllib's
                # default "Python-urllib/x.y" User-Agent as bot traffic --
                # a real UA string is required, not just Content-Type.
                headers={"Content-Type": "application/json", "User-Agent": "warrior-bot (discord-notify, 1.0)"},
                method="POST",
            )
            urllib.request.urlopen(request, timeout=5.0).read()
        except Exception:
            logger.exception("Failed to send Discord notification to channel %r", channel)

    threading.Thread(target=_post, daemon=True).start()


def send_discord_message(content: str, channel: str) -> None:
    _post_payload({"content": content[:_DISCORD_MESSAGE_LIMIT]}, channel)


def _format_signed_dollars(value: float) -> str:
    sign = "+" if value >= 0 else "-"
    return f"{sign}${abs(value):.2f}"


def _pnl_emoji(value: float) -> str:
    return "📈" if value >= 0 else "📉"


def build_pnl_message(symbol: str, trade_pnl: float, daily_pnl: float) -> str:
    """Two lines for the pnl channel: the closing trade's realized P&L,
    then the day's cumulative realized P&L below it -- each prefixed with
    its own up/down chart emoji by its own sign, so a winning trade on an
    otherwise red day still reads correctly (green chart / red chart),
    independent of the other line."""
    return (
        f"{_pnl_emoji(trade_pnl)} {symbol}: {_format_signed_dollars(trade_pnl)}\n"
        f"{_pnl_emoji(daily_pnl)} Daily P&L: {_format_signed_dollars(daily_pnl)}"
    )
