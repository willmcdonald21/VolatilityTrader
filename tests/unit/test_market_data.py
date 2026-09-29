from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from warrior_bot.broker.market_data import ScannerRefused, scan_candidates
from warrior_bot.config import (
    AppConfig, DataWatchdogConfig, ExitsConfig, JournalConfig, KillSwitchConfig,
    LoggingConfig, NotificationsConfig, RiskConfig, ScannerConfig, StrategiesConfig, TradingConfig,
)


class FakeErrorEvent:
    def __init__(self):
        self.listeners = []

    def __iadd__(self, listener):
        self.listeners.append(listener)
        return self

    def __isub__(self, listener):
        self.listeners.remove(listener)
        return self

    def emit(self, *args):
        for listener in list(self.listeners):
            listener(*args)


class FakeScanIB:
    """Emits `errors` through errorEvent during the scan, then returns `rows`."""

    def __init__(self, rows=(), errors=()):
        self._rows = list(rows)
        self._errors = list(errors)
        self.errorEvent = FakeErrorEvent()

    async def reqScannerDataAsync(self, subscription):
        for code, message in self._errors:
            self.errorEvent.emit(-1, code, message, None)
        return self._rows


def _row(symbol):
    return SimpleNamespace(contractDetails=SimpleNamespace(contract=SimpleNamespace(symbol=symbol)))


def _config(tmp_path):
    return AppConfig(
        trading=TradingConfig(),
        risk=RiskConfig(daily_loss_limit_pct=0.02, max_concurrent_positions=3, max_position_pct_of_buying_power=0.25),
        strategies=StrategiesConfig(),
        exits=ExitsConfig(),
        notifications=NotificationsConfig(),
        scanner=ScannerConfig(),
        data_watchdog=DataWatchdogConfig(),
        journal=JournalConfig(db_path=str(tmp_path / "j.sqlite3")),
        kill_switch=KillSwitchConfig(flag_file=str(tmp_path / "K")),
        logging=LoggingConfig(file=str(tmp_path / "l.log")),
    )


def test_normal_scan_returns_symbols(tmp_path):
    ib = FakeScanIB(rows=[_row("AAA"), _row("BBB")])
    assert asyncio.run(scan_candidates(ib, _config(tmp_path))) == ["AAA", "BBB"]
    assert ib.errorEvent.listeners == []  # listener detached


def test_genuinely_empty_scan_is_not_a_refusal(tmp_path):
    # A quiet tape must stay an ordinary empty list, not an exception.
    ib = FakeScanIB(rows=[])
    assert asyncio.run(scan_candidates(ib, _config(tmp_path))) == []


def test_refusal_code_with_empty_result_raises(tmp_path):
    # 2026-09-28: leaked scanner slots made every scan return [] with code
    # 322, logged at INFO as "Scanner returned 0 candidates" -- six hours
    # of blackout indistinguishable from a quiet market.
    ib = FakeScanIB(rows=[], errors=[(322, "Only 10 simultaneous API scanner subscriptions are allowed")])
    with pytest.raises(ScannerRefused) as exc:
        asyncio.run(scan_candidates(ib, _config(tmp_path)))
    assert "322" in str(exc.value)


def test_error_alongside_real_rows_is_not_a_refusal(tmp_path):
    # If the scan returned results, it worked -- an incidental error
    # shouldn't discard them.
    ib = FakeScanIB(rows=[_row("AAA")], errors=[(162, "historical data cancelled")])
    assert asyncio.run(scan_candidates(ib, _config(tmp_path))) == ["AAA"]


def test_unrelated_error_with_empty_result_is_not_a_refusal(tmp_path):
    ib = FakeScanIB(rows=[], errors=[(2104, "Market data farm connection is OK")])
    assert asyncio.run(scan_candidates(ib, _config(tmp_path))) == []


def test_listener_is_detached_even_when_the_request_raises(tmp_path):
    class BoomIB(FakeScanIB):
        async def reqScannerDataAsync(self, subscription):
            raise RuntimeError("connection reset")

    ib = BoomIB()
    with pytest.raises(RuntimeError):
        asyncio.run(scan_candidates(ib, _config(tmp_path)))
    assert ib.errorEvent.listeners == []
