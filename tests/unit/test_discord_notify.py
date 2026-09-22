from __future__ import annotations

import json

import pytest

from warrior_bot.notify import discord as discord_module


class SyncThread:
    """Runs the target synchronously instead of on a real thread, so tests
    don't need to sleep/join to observe the effect."""

    def __init__(self, target, daemon=None):
        self._target = target

    def start(self) -> None:
        self._target()


class FakeResponse:
    def read(self):
        return b""


def _use_sync_thread(monkeypatch):
    monkeypatch.setattr(discord_module, "threading", type("FakeThreadingModule", (), {"Thread": SyncThread}))


def test_unknown_channel_raises():
    with pytest.raises(ValueError):
        discord_module.send_discord_message("hello", channel="not_a_real_channel")


@pytest.mark.parametrize(
    "channel,env_var",
    [
        ("kill_switch", "DISCORD_WEBHOOK_KILL_SWITCH"),
        ("limits", "DISCORD_WEBHOOK_LIMITS"),
        ("trade_activity", "DISCORD_WEBHOOK_TRADE_ACTIVITY"),
    ],
)
def test_no_op_when_that_channels_webhook_url_not_set(monkeypatch, channel, env_var):
    monkeypatch.delenv(env_var, raising=False)
    called = []
    monkeypatch.setattr(discord_module.urllib.request, "urlopen", lambda *a, **k: called.append(1))

    discord_module.send_discord_message("hello", channel=channel)

    assert called == []


def test_posts_content_to_the_correct_channels_webhook(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_KILL_SWITCH", "https://discord.example/kill-switch")
    monkeypatch.setenv("DISCORD_WEBHOOK_LIMITS", "https://discord.example/limits")
    _use_sync_thread(monkeypatch)
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["data"] = json.loads(request.data.decode("utf-8"))
        captured["headers"] = request.headers
        return FakeResponse()

    monkeypatch.setattr(discord_module.urllib.request, "urlopen", fake_urlopen)

    discord_module.send_discord_message("hello world", channel="kill_switch")

    assert captured["url"] == "https://discord.example/kill-switch"
    assert captured["data"]["content"] == "hello world"
    assert captured["headers"]["Content-type"] == "application/json"


def test_truncates_content_to_discord_message_limit(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_TRADE_ACTIVITY", "https://discord.example/trade-activity")
    _use_sync_thread(monkeypatch)
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["data"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse()

    monkeypatch.setattr(discord_module.urllib.request, "urlopen", fake_urlopen)

    discord_module.send_discord_message("x" * 3000, channel="trade_activity")

    assert len(captured["data"]["content"]) == 2000


def test_swallows_exceptions_from_failed_request(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_LIMITS", "https://discord.example/limits")
    _use_sync_thread(monkeypatch)

    def failing_urlopen(*args, **kwargs):
        raise OSError("network down")

    monkeypatch.setattr(discord_module.urllib.request, "urlopen", failing_urlopen)

    discord_module.send_discord_message("hello", channel="limits")  # must not raise


def test_no_op_when_pnl_webhook_url_not_set(monkeypatch):
    monkeypatch.delenv("DISCORD_WEBHOOK_PNL", raising=False)
    called = []
    monkeypatch.setattr(discord_module.urllib.request, "urlopen", lambda *a, **k: called.append(1))

    discord_module.send_discord_message("AAPL: +$1.00", channel="pnl")

    assert called == []


def test_build_pnl_message_green_chart_for_gains():
    message = discord_module.build_pnl_message("AAPL", trade_pnl=114.0, daily_pnl=340.5)

    assert message == "📈 AAPL: +$114.00\n📈 Daily P&L: +$340.50"


def test_build_pnl_message_red_chart_for_losses():
    message = discord_module.build_pnl_message("AAPL", trade_pnl=-50.25, daily_pnl=-12.0)

    assert message == "📉 AAPL: -$50.25\n📉 Daily P&L: -$12.00"


def test_build_pnl_message_independent_emoji_per_line():
    # a winning trade on a red day overall -- each line's emoji reflects
    # its own sign, not one indicator for the whole message
    message = discord_module.build_pnl_message("AAPL", trade_pnl=25.0, daily_pnl=-200.0)

    lines = message.split("\n")
    assert lines[0].startswith("📈")
    assert lines[1].startswith("📉")


def test_build_pnl_message_zero_is_treated_as_green():
    message = discord_module.build_pnl_message("AAPL", trade_pnl=0.0, daily_pnl=0.0)

    assert message.startswith("📈 AAPL: +$0.00")


def test_send_discord_embed_posts_embeds_payload(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_TRADE_ACTIVITY_SUMMARY", "https://discord.example/trade-activity-summary")
    _use_sync_thread(monkeypatch)
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["data"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse()

    monkeypatch.setattr(discord_module.urllib.request, "urlopen", fake_urlopen)

    discord_module.send_discord_embed({"title": "hi"}, channel="trade_activity_summary")

    assert captured["url"] == "https://discord.example/trade-activity-summary"
    assert captured["data"] == {"embeds": [{"title": "hi"}]}


def test_no_op_when_entry_summary_webhook_url_not_set(monkeypatch):
    monkeypatch.delenv("DISCORD_WEBHOOK_TRADE_ACTIVITY_SUMMARY", raising=False)
    called = []
    monkeypatch.setattr(discord_module.urllib.request, "urlopen", lambda *a, **k: called.append(1))

    discord_module.send_discord_embed({"title": "hi"}, channel="trade_activity_summary")

    assert called == []


# -- build_entry_summary_embed --


def test_build_entry_summary_embed_new_position_basics():
    embed = discord_module.build_entry_summary_embed(
        symbol="JTAI",
        strategy="gap_and_go",
        qty=1482.0,
        avg_price=2.11,
        stop_price=2.02,
        trim_targets=[(0.34, 2.19), (0.33, 2.27)],
        mode="paper",
    )

    assert "NEW POSITION" in embed["title"]
    assert "JTAI" in embed["title"]
    assert "gap_and_go" in embed["title"]
    field_by_name = {f["name"]: f["value"] for f in embed["fields"]}
    assert field_by_name["Avg Price"] == "$2.11"
    assert field_by_name["Shares"] == "1482"
    assert field_by_name["Cost"] == "$3,127.02"
    assert field_by_name["Stop"] == "$2.02"
    assert field_by_name["Trim Targets"] == "34% @ $2.19\n33% @ $2.27"
    assert embed["footer"]["text"] == "Paper trade · data via IBKR · Not financial advice"


def test_build_entry_summary_embed_live_mode_says_real_trade():
    embed = discord_module.build_entry_summary_embed(
        symbol="JTAI", strategy="gap_and_go", qty=100.0, avg_price=2.0,
        stop_price=1.9, trim_targets=[], mode="live",
    )

    assert embed["footer"]["text"].startswith("Real trade")


def test_build_entry_summary_embed_addon_shows_blended_average():
    embed = discord_module.build_entry_summary_embed(
        symbol="GDC",
        strategy="bull_flag",
        qty=100.0,
        avg_price=2.20,
        stop_price=2.05,
        trim_targets=[(1.0, 2.40)],
        mode="paper",
        prior_qty=200.0,
        prior_avg_price=2.00,
    )

    assert "ADD TO POSITION" in embed["title"]
    # blended = (200*2.00 + 100*2.20) / 300 = 2.0667 -> $2.07
    assert "$2.00 → $2.07" in embed["description"]
    assert "200 → 300 shares" in embed["description"]
    field_by_name = {f["name"]: f["value"] for f in embed["fields"]}
    assert field_by_name["Shares"] == "300"  # combined, not just this lot
    assert field_by_name["Avg Price"] == "$2.07"
    assert "Trim Targets (this lot)" in field_by_name  # only this lot's own tiers, not merged


def test_build_entry_summary_embed_no_trim_targets_field_when_empty():
    embed = discord_module.build_entry_summary_embed(
        symbol="X", strategy="s", qty=1.0, avg_price=1.0, stop_price=0.9, trim_targets=[], mode="paper",
    )

    assert all(f["name"] not in ("Trim Targets", "Trim Targets (this lot)") for f in embed["fields"])


def test_build_entry_summary_embed_new_position_uses_green_addon_uses_amber():
    new_embed = discord_module.build_entry_summary_embed(
        symbol="X", strategy="s", qty=1.0, avg_price=1.0, stop_price=0.9, trim_targets=[], mode="paper",
    )
    addon_embed = discord_module.build_entry_summary_embed(
        symbol="X", strategy="s", qty=1.0, avg_price=1.0, stop_price=0.9, trim_targets=[], mode="paper",
        prior_qty=1.0, prior_avg_price=1.0,
    )

    assert new_embed["color"] != addon_embed["color"]
