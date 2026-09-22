from __future__ import annotations

import json
import logging
import os
import threading
import urllib.request
from datetime import datetime, timezone

logger = logging.getLogger("warrior_bot.notify.discord")

_DISCORD_MESSAGE_LIMIT = 2000

# Five separate channels, each its own webhook -- kill-switch/connection
# events, daily-limit/EOD "trading stopped" events, routine trade activity
# (every individual signal + fill line, unchanged/raw), a curated one-line-
# per-entry-burst summary (see build_entry_summary_embed), and per-trade/
# daily P&L, each land in their own place rather than one noisy firehose.
_CHANNEL_ENV_VARS = {
    "kill_switch": "DISCORD_WEBHOOK_KILL_SWITCH",
    "limits": "DISCORD_WEBHOOK_LIMITS",
    "trade_activity": "DISCORD_WEBHOOK_TRADE_ACTIVITY",
    "trade_activity_summary": "DISCORD_WEBHOOK_TRADE_ACTIVITY_SUMMARY",
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


def send_discord_embed(embed: dict, channel: str) -> None:
    _post_payload({"embeds": [embed]}, channel)


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


# Colors for build_entry_summary_embed -- a fresh entry vs. a pyramid
# add-on read differently at a glance even before the title text registers.
_ENTRY_COLOR = 0x2ECC71  # green
_ADDON_COLOR = 0xF5A623  # amber


def build_entry_summary_embed(
    symbol: str,
    strategy: str,
    qty: float,
    avg_price: float,
    stop_price: float,
    trim_targets: list[tuple[float, float]],
    mode: str,
    prior_qty: float | None = None,
    prior_avg_price: float | None = None,
) -> dict:
    """One Discord embed summarizing a completed entry-fill burst (see
    OrderManager._send_entry_summary, which debounces/coalesces a burst of
    partial fills on the parent order into a single call here) -- replaces
    a wall of "BUY {symbol} {qty} @ {price}" lines, one per partial fill,
    with one message per entry.

    `trim_targets` is [(pct_of_position, price), ...] already resolved by
    the caller from the *actual* profit-tier orders placed for this specific
    lot (not recomputed here) -- e.g. [(0.34, 2.19), (0.33, 2.27)]. Empty
    when the strategy has no configured tiers (a single full-quantity
    target instead); the caller passes that one target the same way with
    a single (1.0, target_price) entry so this function doesn't need to
    know the difference.

    `prior_qty`/`prior_avg_price` being set means this is a pyramid add-on
    (a second lot on a symbol already held) -- the caller resolves this by
    checking PositionManager.other_open_lot before calling. Deliberately
    doesn't try to merge the two lots' separate trim-target orders into one
    blended schedule (they're genuinely separate resting orders, each sized
    off its own lot's entry) -- `trim_targets` always describes only the
    lot that just filled.
    """
    is_addon = prior_qty is not None and prior_avg_price is not None

    if is_addon:
        total_qty = prior_qty + qty
        blended_avg = (prior_qty * prior_avg_price + qty * avg_price) / total_qty
        title = f"\U0001f53c ADD TO POSITION — {symbol} · {strategy}"
        description = (
            f"Added **{qty:g} @ ${avg_price:.2f}** to {symbol}\n"
            f"Average ${prior_avg_price:.2f} → ${blended_avg:.2f} · "
            f"{prior_qty:g} → {total_qty:g} shares"
        )
        display_qty, display_avg, color = total_qty, blended_avg, _ADDON_COLOR
    else:
        title = f"\U0001f7e2 NEW POSITION — {symbol} · {strategy}"
        description = f"Entered **{symbol}** — filled **{qty:g} @ ${avg_price:.2f}**"
        display_qty, display_avg, color = qty, avg_price, _ENTRY_COLOR

    fields = [
        {"name": "Avg Price", "value": f"${display_avg:.2f}", "inline": True},
        {"name": "Shares", "value": f"{display_qty:g}", "inline": True},
        {"name": "Cost", "value": f"${display_qty * display_avg:,.2f}", "inline": True},
        {"name": "Stop", "value": f"${stop_price:.2f}", "inline": True},
    ]
    if trim_targets:
        label = "Trim Targets" + (" (this lot)" if is_addon else "")
        lines = [f"{pct * 100:.0f}% @ ${price:.2f}" for pct, price in trim_targets]
        fields.append({"name": label, "value": "\n".join(lines), "inline": False})

    mode_label = "Real trade" if mode == "live" else "Paper trade"
    return {
        "title": title,
        "description": description,
        "color": color,
        "fields": fields,
        "footer": {"text": f"{mode_label} · data via IBKR · Not financial advice"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
