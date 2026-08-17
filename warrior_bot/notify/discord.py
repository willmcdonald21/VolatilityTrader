from __future__ import annotations

import json
import logging
import os
import threading
import urllib.request

logger = logging.getLogger("warrior_bot.notify.discord")

_DISCORD_MESSAGE_LIMIT = 2000

# Three separate channels, each its own webhook -- kill-switch/connection
# events, daily-limit/EOD "trading stopped" events, and routine trade
# activity (signals + fills) each land in their own place rather than one
# noisy firehose.
_CHANNEL_ENV_VARS = {
    "kill_switch": "DISCORD_WEBHOOK_KILL_SWITCH",
    "limits": "DISCORD_WEBHOOK_LIMITS",
    "trade_activity": "DISCORD_WEBHOOK_TRADE_ACTIVITY",
}


def send_discord_message(content: str, channel: str) -> None:
    """Fire-and-forget post to one of the named webhook channels (see
    _CHANNEL_ENV_VARS), read from environment variables -- deliberately
    never from config.yaml, which is tracked in git. No-ops silently if
    that channel's variable isn't set, so notifications are opt-in with
    zero setup cost otherwise.

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
            data = json.dumps({"content": content[:_DISCORD_MESSAGE_LIMIT]}).encode("utf-8")
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
