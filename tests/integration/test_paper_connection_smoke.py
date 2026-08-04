"""Requires a running IB Gateway (or TWS) paper session on the configured
host/port with API access enabled. Skips itself if no session is reachable
so `pytest` still passes cleanly in environments without Gateway running
(e.g. CI, or before Phase 0 setup is complete)."""

from __future__ import annotations

import pytest
from ib_async import IB

from warrior_bot.config import load_config


@pytest.fixture
def config():
    return load_config()


@pytest.fixture
def ib(config):
    client = IB()
    try:
        client.connect(config.trading.host, config.trading.port, clientId=config.trading.client_id + 999, timeout=4)
    except Exception as exc:
        pytest.skip(f"No IB Gateway/TWS reachable at {config.trading.host}:{config.trading.port}: {exc}")
    yield client
    client.disconnect()


def test_connects_and_reports_account_summary(ib, config):
    assert config.trading.mode == "paper", "Integration smoke test refuses to run against a live-configured account"
    assert ib.isConnected()
    account_values = ib.accountSummary()
    assert isinstance(account_values, list)


def test_can_list_positions(ib):
    positions = ib.positions()
    assert isinstance(positions, list)
