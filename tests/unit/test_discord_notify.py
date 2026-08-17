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
