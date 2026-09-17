from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from warrior_bot.config import (
    AppConfig,
    DataWatchdogConfig,
    ExitsConfig,
    JournalConfig,
    KillSwitchConfig,
    LoggingConfig,
    NotificationsConfig,
    RiskConfig,
    ScannerConfig,
    StrategiesConfig,
    TradingConfig,
)
from warrior_bot.main import WarriorBot
from warrior_bot.risk.account_state import AccountSnapshot
from warrior_bot.signals.signal import Signal
from warrior_bot.utils.time_utils import to_eastern


def make_config(tmp_path, data_watchdog: DataWatchdogConfig | None = None) -> AppConfig:
    return AppConfig(
        trading=TradingConfig(),
        risk=RiskConfig(
            daily_loss_limit_pct=0.02,
            max_concurrent_positions=3,
            max_position_pct_of_buying_power=0.25,
        ),
        strategies=StrategiesConfig(),
        exits=ExitsConfig(),
        notifications=NotificationsConfig(),
        scanner=ScannerConfig(),
        data_watchdog=data_watchdog or DataWatchdogConfig(),
        journal=JournalConfig(db_path=str(tmp_path / "journal.sqlite3")),
        kill_switch=KillSwitchConfig(flag_file=str(tmp_path / "KILL_SWITCH")),
        logging=LoggingConfig(file=str(tmp_path / "warrior_bot.log")),
    )


def test_on_connected_is_noop_on_first_connect(tmp_path):
    bot = WarriorBot(make_config(tmp_path))

    # first connect: nothing tracked yet, nothing to drop
    bot._on_connected()

    assert bot.contexts == {}
    assert bot.contracts == {}
    assert bot._subscriptions == {}


def test_on_connected_drops_tracked_symbols_after_reconnect(tmp_path):
    bot = WarriorBot(make_config(tmp_path))
    bot.contexts["AAPL"] = object()
    bot.contracts["AAPL"] = object()
    bot._subscriptions["AAPL"] = object()

    bot._on_connected()

    # dropped, not re-subscribed here -- _scan_loop treats AAPL as new again
    # on its next iteration and calls _onboard_symbol for it
    assert bot.contexts == {}
    assert bot.contracts == {}
    assert bot._subscriptions == {}


def test_on_connected_does_not_touch_position_manager(tmp_path):
    bot = WarriorBot(make_config(tmp_path))
    bot.contexts["AAPL"] = object()
    bot.position_manager._positions["AAPL"] = object()

    bot._on_connected()

    # standing bracket orders are IBKR's problem to keep working, not ours
    # to re-establish -- only the scanning/bar-subscription side is reset
    assert "AAPL" in bot.position_manager._positions


def make_signal(entry=10.0, stop=9.0) -> Signal:
    return Signal(
        symbol="AAPL",
        strategy="gap_and_go",
        side="BUY",
        entry_price=entry,
        stop_price=stop,
        target_price=12.0,
        ts=datetime.now(timezone.utc),
    )


def test_clamp_stop_tightens_stop_wider_than_conservative_max(tmp_path):
    bot = WarriorBot(make_config(tmp_path))  # default max_stop_distance_pct=2.0
    signal = make_signal(entry=10.0, stop=9.0)  # risk=1.0 -> 10% of entry, wider than the 2% cap

    bot._clamp_stop_to_conservative_max(signal)

    assert signal.stop_price == 10.0 - (10.0 * 0.02)  # 9.8


def test_clamp_stop_leaves_already_tight_stop_untouched(tmp_path):
    bot = WarriorBot(make_config(tmp_path))
    signal = make_signal(entry=10.0, stop=9.9)  # risk=0.1 -> 1% of entry, already under the 2% cap

    bot._clamp_stop_to_conservative_max(signal)

    assert signal.stop_price == 9.9


def test_clamp_stop_result_is_tick_conformant(tmp_path):
    # entry_price * pct / 100.0 is raw float arithmetic -- entry=2.21,
    # max_stop_distance_pct=2.0 produces stop=2.1658 (4 decimals) unless
    # rounded, which IBKR rejects with error 110 (this is the exact
    # regression seen live against SCNI on 2026-09-14: signal.stop_price is
    # mutated here *after* Signal.__post_init__ already ran, so its own
    # rounding doesn't cover this assignment).
    bot = WarriorBot(make_config(tmp_path))
    signal = make_signal(entry=2.21, stop=2.05)  # risk=0.16 -> ~7.2% of entry, wider than the 2% cap

    bot._clamp_stop_to_conservative_max(signal)

    assert signal.stop_price == round(signal.stop_price, 2)


def _fake_snapshot(open_positions_count=0) -> AccountSnapshot:
    return AccountSnapshot(
        net_liquidation=10_000,
        available_funds=10_000,
        buying_power=10_000,
        open_positions_count=open_positions_count,
        daily_realized_pnl=0.0,
    )


def _today_et():
    return to_eastern(datetime.now(timezone.utc)).date()


def test_check_new_trading_day_noop_on_same_day(tmp_path):
    bot = WarriorBot(make_config(tmp_path))
    bot.account_state.snapshot = lambda: _fake_snapshot()
    bot._trading_day = _today_et()
    bot._flattened_today = True

    bot._check_new_trading_day()

    assert bot._flattened_today is True  # unchanged -- still the same trading day


def test_check_new_trading_day_resets_flattened_today_flag(tmp_path):
    bot = WarriorBot(make_config(tmp_path))
    bot.account_state.snapshot = lambda: _fake_snapshot()
    bot._trading_day = (datetime.now(timezone.utc) - timedelta(days=1)).date()
    bot._flattened_today = True  # simulates yesterday's EOD flatten having already fired

    bot._check_new_trading_day()

    assert bot._flattened_today is False  # today's own EOD flatten can fire again
    assert bot._trading_day == _today_et()


def test_check_new_trading_day_alerts_when_positions_still_open(tmp_path, monkeypatch):
    alerts = []
    monkeypatch.setattr("warrior_bot.main.alert", lambda message, channel=None: alerts.append((message, channel)))
    bot = WarriorBot(make_config(tmp_path))
    bot.account_state.snapshot = lambda: _fake_snapshot(open_positions_count=2)
    bot._trading_day = (datetime.now(timezone.utc) - timedelta(days=1)).date()

    bot._check_new_trading_day()

    assert len(alerts) == 1
    message, channel = alerts[0]
    assert "2 position" in message
    assert channel == "limits"


def test_check_new_trading_day_no_alert_when_no_positions_open(tmp_path, monkeypatch):
    alerts = []
    monkeypatch.setattr("warrior_bot.main.alert", lambda message, channel=None: alerts.append((message, channel)))
    bot = WarriorBot(make_config(tmp_path))
    bot.account_state.snapshot = lambda: _fake_snapshot(open_positions_count=0)
    bot._trading_day = (datetime.now(timezone.utc) - timedelta(days=1)).date()

    bot._check_new_trading_day()

    assert alerts == []


# -- data watchdog: capacity management, staleness detection, resubscribe --
# Regression coverage for the 2026-09-15 incident: RETO (that day's #1
# scanner-ranked gainer) and several other symbols (WAFU/WNW/FTFT/ARTL/
# BGMS/SKIL) each got one onboarding log line and then produced zero
# further strategy evaluations for the rest of the session. See
# DataWatchdogConfig in warrior_bot/config.py for the full incident writeup.


def _seed_symbol(bot: WarriorBot, symbol: str, *, scan_seen_at: datetime | None = None) -> None:
    """Registers `symbol` as already tracked (context/contract/subscription
    all present) -- the steady-state every helper below expects to find a
    symbol in before acting on it."""
    bot.contexts[symbol] = SimpleNamespace(symbol=symbol)
    bot.contracts[symbol] = SimpleNamespace(symbol=symbol)
    bot._subscriptions[symbol] = SimpleNamespace(reqId=hash(symbol) % 10_000)
    bot._last_bar_at[symbol] = datetime.now(timezone.utc)
    bot._last_scan_seen_at[symbol] = scan_seen_at if scan_seen_at is not None else datetime.now(timezone.utc)


def _watchdog_config(**overrides) -> DataWatchdogConfig:
    return DataWatchdogConfig(**overrides)


def test_ensure_capacity_true_when_under_cap(tmp_path):
    cfg = _watchdog_config(max_concurrent_subscriptions=5)
    bot = WarriorBot(make_config(tmp_path, data_watchdog=cfg))
    _seed_symbol(bot, "AAA")

    assert bot._ensure_subscription_capacity("NEW") is True
    assert "AAA" in bot._subscriptions  # untouched -- there was room, nothing evicted


def test_ensure_capacity_evicts_least_recently_scan_relevant_symbol_at_cap(tmp_path):
    cfg = _watchdog_config(max_concurrent_subscriptions=2, inactive_unsubscribe_seconds=1)
    bot = WarriorBot(make_config(tmp_path, data_watchdog=cfg))
    now = datetime.now(timezone.utc)
    _seed_symbol(bot, "STALE", scan_seen_at=now - timedelta(hours=2))
    _seed_symbol(bot, "FRESHER", scan_seen_at=now - timedelta(minutes=1))
    bot.ib.cancelHistoricalData = lambda bars: None

    assert bot._ensure_subscription_capacity("NEW") is True

    assert "STALE" not in bot._subscriptions  # oldest scanner appearance evicted first
    assert "FRESHER" in bot._subscriptions


def test_ensure_capacity_never_evicts_symbol_with_open_position(tmp_path):
    cfg = _watchdog_config(max_concurrent_subscriptions=1, inactive_unsubscribe_seconds=1)
    bot = WarriorBot(make_config(tmp_path, data_watchdog=cfg))
    _seed_symbol(bot, "HELD", scan_seen_at=datetime.now(timezone.utc) - timedelta(hours=2))
    bot.position_manager._positions["HELD"] = [object()]  # open lot -- must never be evicted for capacity

    assert bot._ensure_subscription_capacity("NEW") is False
    assert "HELD" in bot._subscriptions


def test_ensure_capacity_never_evicts_recently_scan_relevant_symbol(tmp_path):
    cfg = _watchdog_config(max_concurrent_subscriptions=1, inactive_unsubscribe_seconds=1800)
    bot = WarriorBot(make_config(tmp_path, data_watchdog=cfg))
    _seed_symbol(bot, "HOT", scan_seen_at=datetime.now(timezone.utc))  # still in this tick's top-N

    assert bot._ensure_subscription_capacity("NEW") is False
    assert "HOT" in bot._subscriptions


def test_ensure_capacity_false_and_logs_when_nothing_evictable(tmp_path, caplog):
    cfg = _watchdog_config(max_concurrent_subscriptions=1, inactive_unsubscribe_seconds=1800)
    bot = WarriorBot(make_config(tmp_path, data_watchdog=cfg))
    _seed_symbol(bot, "HOT")

    with caplog.at_level("WARNING"):
        result = bot._ensure_subscription_capacity("NEW")

    assert result is False
    assert any("skipping onboarding" in r.message for r in caplog.records)


def test_ensure_capacity_incoming_symbol_never_evicts_itself(tmp_path):
    # A symbol already tracked that reappears as its own "incoming" candidate
    # (shouldn't normally happen -- _scan_loop only onboards symbols not
    # already in self.contexts -- but _pick_eviction_candidate excludes it
    # defensively) must never be offered up as its own eviction victim.
    cfg = _watchdog_config(max_concurrent_subscriptions=1, inactive_unsubscribe_seconds=1)
    bot = WarriorBot(make_config(tmp_path, data_watchdog=cfg))
    _seed_symbol(bot, "SELF", scan_seen_at=datetime.now(timezone.utc) - timedelta(hours=2))

    assert bot._pick_eviction_candidate("SELF") is None


def test_unsubscribe_symbol_cancels_and_clears_all_tracking(tmp_path):
    bot = WarriorBot(make_config(tmp_path))
    _seed_symbol(bot, "AAA")
    cancelled = []
    bot.ib.cancelHistoricalData = lambda bars: cancelled.append(bars)

    bot._unsubscribe_symbol("AAA", reason="capacity")

    assert len(cancelled) == 1
    assert "AAA" not in bot.contexts
    assert "AAA" not in bot.contracts
    assert "AAA" not in bot._subscriptions
    assert "AAA" not in bot._last_bar_at
    assert "AAA" not in bot._last_scan_seen_at


def test_unsubscribe_symbol_still_clears_local_state_if_cancel_raises(tmp_path):
    # cancelHistoricalData reaching IBKR for an already-dead subscription is
    # exactly the kind of thing that can raise -- local bookkeeping must not
    # get stuck just because the broker-side cancel failed.
    bot = WarriorBot(make_config(tmp_path))
    _seed_symbol(bot, "AAA")

    def _raise(bars):
        raise RuntimeError("not connected")

    bot.ib.cancelHistoricalData = _raise

    bot._unsubscribe_symbol("AAA", reason="capacity")

    assert "AAA" not in bot._subscriptions
    assert "AAA" not in bot.contexts


def test_bar_update_handler_stamps_last_bar_at_on_every_call(tmp_path):
    bot = WarriorBot(make_config(tmp_path))
    ctx = SimpleNamespace(add_bar=lambda b: None)
    contract = SimpleNamespace()
    handler = bot._make_bar_update_handler("AAA", contract, ctx)

    handler([], False)  # interim tick, no new bar -- still proof the feed is alive

    assert "AAA" in bot._last_bar_at


def test_bar_update_handler_invokes_on_new_bar_when_has_new_bar(tmp_path):
    bot = WarriorBot(make_config(tmp_path))
    added_bars = []
    ctx = SimpleNamespace(add_bar=lambda b: added_bars.append(b))
    contract = SimpleNamespace()
    calls = []
    bot._on_new_bar = lambda c, x: calls.append((c, x))
    fake_bar = SimpleNamespace(date=None, open=1, high=1, low=1, close=1, volume=1)
    handler = bot._make_bar_update_handler("AAA", contract, ctx)

    handler([fake_bar], True)

    assert len(added_bars) == 1
    assert calls == [(contract, ctx)]


def test_bar_update_handler_swallows_exception_without_propagating(tmp_path):
    # An uncaught exception here would otherwise be able to silently kill
    # this listener inside eventkit -- exactly the failure mode under
    # investigation, so this callback must never be the one link in the
    # chain without a safety net (unlike _on_new_bar's per-strategy loop,
    # which already had one before this fix).
    bot = WarriorBot(make_config(tmp_path))
    ctx = SimpleNamespace(add_bar=lambda b: None)
    contract = SimpleNamespace()
    bot._on_new_bar = lambda c, x: (_ for _ in ()).throw(RuntimeError("boom"))
    fake_bar = SimpleNamespace(date=None, open=1, high=1, low=1, close=1, volume=1)
    handler = bot._make_bar_update_handler("AAA", contract, ctx)

    handler([fake_bar], True)  # must not raise

    assert "AAA" in bot._last_bar_at  # liveness still recorded despite the downstream failure


def test_check_stale_subscriptions_noop_outside_active_session(tmp_path, monkeypatch):
    monkeypatch.setattr("warrior_bot.main.is_active_session", lambda: False)
    cfg = _watchdog_config(stale_after_seconds=1)
    bot = WarriorBot(make_config(tmp_path, data_watchdog=cfg))
    _seed_symbol(bot, "AAA", scan_seen_at=datetime.now(timezone.utc))
    bot._last_bar_at["AAA"] = datetime.now(timezone.utc) - timedelta(hours=1)
    resubscribed = []
    bot._resubscribe_symbol = lambda s: resubscribed.append(s) or _completed_future()

    asyncio.run(bot._check_stale_subscriptions())

    assert resubscribed == []


def _completed_future():
    async def _noop():
        return None

    return _noop()


def test_check_stale_subscriptions_leaves_fresh_symbol_alone(tmp_path, monkeypatch):
    monkeypatch.setattr("warrior_bot.main.is_active_session", lambda: True)
    cfg = _watchdog_config(stale_after_seconds=180)
    bot = WarriorBot(make_config(tmp_path, data_watchdog=cfg))
    _seed_symbol(bot, "AAA")  # last_bar_at defaults to "now" -- well within threshold
    resubscribed = []

    async def _fake_resubscribe(symbol):
        resubscribed.append(symbol)

    bot._resubscribe_symbol = _fake_resubscribe

    asyncio.run(bot._check_stale_subscriptions())

    assert resubscribed == []


def test_check_stale_subscriptions_resubscribes_symbol_past_threshold(tmp_path, monkeypatch):
    monkeypatch.setattr("warrior_bot.main.is_active_session", lambda: True)
    cfg = _watchdog_config(stale_after_seconds=60)
    bot = WarriorBot(make_config(tmp_path, data_watchdog=cfg))
    _seed_symbol(bot, "STALE")
    bot._last_bar_at["STALE"] = datetime.now(timezone.utc) - timedelta(seconds=600)
    _seed_symbol(bot, "FRESH")
    resubscribed = []

    async def _fake_resubscribe(symbol):
        resubscribed.append(symbol)

    bot._resubscribe_symbol = _fake_resubscribe

    asyncio.run(bot._check_stale_subscriptions())

    assert resubscribed == ["STALE"]


def test_resubscribe_symbol_cancels_old_and_installs_new_subscription(tmp_path):
    bot = WarriorBot(make_config(tmp_path))
    _seed_symbol(bot, "AAA")
    old_sub = bot._subscriptions["AAA"]
    cancelled = []
    bot.ib.cancelHistoricalData = lambda bars: cancelled.append(bars)
    new_sub = SimpleNamespace(updateEvent=_FakeEventForMain())

    async def _fake_request(contract):
        return new_sub

    bot._request_live_updates = _fake_request

    asyncio.run(bot._resubscribe_symbol("AAA"))

    assert cancelled == [old_sub]
    assert bot._subscriptions["AAA"] is new_sub
    assert "AAA" in bot._last_bar_at


class _FakeEventForMain:
    def __iadd__(self, listener):
        return self


def test_resubscribe_symbol_noop_if_symbol_no_longer_tracked(tmp_path):
    bot = WarriorBot(make_config(tmp_path))
    # never seeded -- simulates a race where the symbol was evicted/dropped
    # between the watchdog snapshotting stale symbols and acting on them

    async def _fake_request(contract):
        raise AssertionError("must not attempt to resubscribe an untracked symbol")

    bot._request_live_updates = _fake_request

    asyncio.run(bot._resubscribe_symbol("GHOST"))  # must not raise


def test_resubscribe_symbol_backs_off_on_failure_instead_of_tight_retry(tmp_path):
    bot = WarriorBot(make_config(tmp_path))
    _seed_symbol(bot, "AAA")
    before = datetime.now(timezone.utc) - timedelta(seconds=600)
    bot._last_bar_at["AAA"] = before

    async def _fake_request(contract):
        raise RuntimeError("pacing violation")

    bot._request_live_updates = _fake_request

    asyncio.run(bot._resubscribe_symbol("AAA"))

    assert "AAA" not in bot._subscriptions  # old one popped, no new one installed
    assert bot._last_bar_at["AAA"] > before  # stamped fresh so the watchdog waits a full cycle, not the next tick


def test_resubscribe_symbol_survives_cancel_failure_and_still_resubscribes(tmp_path):
    bot = WarriorBot(make_config(tmp_path))
    _seed_symbol(bot, "AAA")

    def _raise(bars):
        raise RuntimeError("already dead server-side")

    bot.ib.cancelHistoricalData = _raise
    new_sub = SimpleNamespace(updateEvent=_FakeEventForMain())

    async def _fake_request(contract):
        return new_sub

    bot._request_live_updates = _fake_request

    asyncio.run(bot._resubscribe_symbol("AAA"))

    assert bot._subscriptions["AAA"] is new_sub


# -- _onboard_symbol regressions --


def _patch_onboarding_prereqs(monkeypatch, bot: WarriorBot, contract=None):
    contract = contract or SimpleNamespace(symbol="NEW")

    async def _qualify(symbol):
        return contract

    async def _prior_close(ib, c):
        return None

    async def _warmup(ib, c, config):
        return []

    bot.ib_client.qualify_stock = _qualify
    monkeypatch.setattr("warrior_bot.main.fetch_prior_close", _prior_close)
    monkeypatch.setattr("warrior_bot.main.fetch_warmup_bars", _warmup)

    async def _reqHistoricalDataAsync(*args, **kwargs):
        raise RuntimeError("not connected")  # daily_bars fetch -- caught internally, logged, continues

    bot.ib.reqHistoricalDataAsync = _reqHistoricalDataAsync
    return contract


def test_onboard_symbol_rolls_back_contexts_when_live_subscribe_fails(tmp_path, monkeypatch):
    # The exact bug behind the 2026-09-15 incident for the "request fails
    # outright" case: contexts[symbol] was set before the live-subscribe
    # call, so _scan_loop's `if symbol not in self.contexts` gate treated a
    # symbol that never got a working subscription as already handled --
    # forever. It must instead be eligible for another onboarding attempt.
    bot = WarriorBot(make_config(tmp_path))
    _patch_onboarding_prereqs(monkeypatch, bot)

    async def _fail(contract):
        raise RuntimeError("Failed to request live updates (disconnected)")

    bot._request_live_updates = _fail

    asyncio.run(bot._onboard_symbol("NEW", scanner_rank=1))

    assert "NEW" not in bot.contexts
    assert "NEW" not in bot.contracts
    assert "NEW" not in bot._subscriptions


def test_onboard_symbol_succeeds_and_stamps_last_bar_at(tmp_path, monkeypatch):
    bot = WarriorBot(make_config(tmp_path))
    _patch_onboarding_prereqs(monkeypatch, bot)
    new_sub = SimpleNamespace(updateEvent=_FakeEventForMain())

    async def _ok(contract):
        return new_sub

    bot._request_live_updates = _ok

    asyncio.run(bot._onboard_symbol("NEW", scanner_rank=1))

    assert bot.contexts["NEW"] is not None
    assert bot._subscriptions["NEW"] is new_sub
    assert "NEW" in bot._last_bar_at


def test_onboard_symbol_skips_entirely_when_at_capacity_and_nothing_evictable(tmp_path, monkeypatch):
    cfg = _watchdog_config(max_concurrent_subscriptions=1, inactive_unsubscribe_seconds=1800)
    bot = WarriorBot(make_config(tmp_path, data_watchdog=cfg))
    _seed_symbol(bot, "HOT")  # occupies the only slot, still scan-relevant -- ineligible for eviction
    qualify_called = []

    async def _qualify(symbol):
        qualify_called.append(symbol)
        raise AssertionError("should never reach contract qualification once capacity check fails")

    bot.ib_client.qualify_stock = _qualify

    asyncio.run(bot._onboard_symbol("NEW", scanner_rank=1))

    assert qualify_called == []  # bailed out before doing any work at all
    assert "NEW" not in bot.contexts

