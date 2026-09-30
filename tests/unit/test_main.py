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


def make_config(
    tmp_path, data_watchdog: DataWatchdogConfig | None = None, scanner: ScannerConfig | None = None
) -> AppConfig:
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
        scanner=scanner or ScannerConfig(),
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


def test_on_connected_does_not_clear_position_manager_tracking(tmp_path):
    # Unlike bar subscriptions (wiped and re-onboarded from scratch), a
    # reconnect must never lose track of a real open position -- it only
    # needs its fill *listeners* resynced onto fresh Trade objects (see
    # PositionManager.resync_after_reconnect), which is a no-op here since
    # there are no real orders at IBKR for this fake lot to find.
    bot = WarriorBot(make_config(tmp_path))
    bot.contexts["AAPL"] = object()
    bot.position_manager._positions["AAPL"] = []

    bot._on_connected()

    assert "AAPL" in bot.position_manager._positions


def test_on_connected_resyncs_order_and_position_tracking(tmp_path, monkeypatch):
    bot = WarriorBot(make_config(tmp_path))
    bot.contexts["AAPL"] = object()
    calls = []
    bot.position_manager.resync_after_reconnect = lambda ib: calls.append(("position_manager", ib)) or {5, 6}
    bot.order_manager.resync_open_orders = lambda **kwargs: calls.append(("order_manager", kwargs))

    bot._on_connected()

    assert calls[0] == ("position_manager", bot.ib)
    assert calls[1] == ("order_manager", {"force": True, "skip_order_ids": frozenset({5, 6})})


def test_on_connected_always_resyncs_even_with_no_tracked_symbols(tmp_path):
    # Changed 2026-09-28. The early return for an empty `contexts` used to
    # skip BOTH the subscription teardown and the two resyncs -- but the
    # resyncs recover fill listeners for OPEN POSITIONS, which can exist
    # while contexts is empty (right after reset_daily_state, after a mass
    # eviction, or before the first scan tick completes). Skipping them
    # there left those positions permanently unmanaged. On a genuine first
    # connect they are harmless no-ops.
    bot = WarriorBot(make_config(tmp_path))
    calls = []
    bot.position_manager.resync_after_reconnect = lambda ib: calls.append("position_manager") or set()
    bot.order_manager.resync_open_orders = lambda **kwargs: calls.append("order_manager")

    bot._on_connected()

    assert calls == ["position_manager", "order_manager"]
    assert bot._resubscribe_after_reconnect == set()  # nothing held, nothing to re-onboard


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


def _yesterday_et():
    """Derived from the ET date, not the UTC one -- _check_new_trading_day
    compares ET dates, and after 8pm ET the UTC date has already rolled over,
    so a UTC-based "yesterday" equals today in ET and these tests silently
    stop testing a day change at all."""
    return _today_et() - timedelta(days=1)


def test_check_new_trading_day_noop_on_same_day(tmp_path):
    bot = WarriorBot(make_config(tmp_path))
    bot.account_state.snapshot = lambda: _fake_snapshot()
    bot._trading_day = _today_et()
    bot._eod_flatten_fired = True
    bot._loss_limit_flatten_fired = True

    bot._check_new_trading_day()

    assert bot._eod_flatten_fired is True  # unchanged -- still the same trading day
    assert bot._loss_limit_flatten_fired is True


def test_check_new_trading_day_resets_flatten_flags(tmp_path):
    bot = WarriorBot(make_config(tmp_path))
    bot.account_state.snapshot = lambda: _fake_snapshot()
    bot._trading_day = _yesterday_et()
    bot._eod_flatten_fired = True  # simulates yesterday's EOD flatten having already fired
    bot._loss_limit_flatten_fired = True

    bot._check_new_trading_day()

    assert bot._eod_flatten_fired is False  # today's own EOD flatten can fire again
    assert bot._loss_limit_flatten_fired is False
    assert bot._trading_day == _today_et()


def test_check_new_trading_day_alerts_when_positions_still_open(tmp_path, monkeypatch):
    alerts = []
    monkeypatch.setattr("warrior_bot.main.alert", lambda message, channel=None: alerts.append((message, channel)))
    bot = WarriorBot(make_config(tmp_path))
    bot.account_state.snapshot = lambda: _fake_snapshot(open_positions_count=2)
    bot._trading_day = _yesterday_et()

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
    bot._trading_day = _yesterday_et()

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
    # Two bars: bars[-2] (closed) is the one _make_bar_update_handler must
    # add -- see test_bar_update_handler_adds_the_closed_bar_not_the_new_one
    # for the regression this guards against.
    closed_bar = SimpleNamespace(date=None, open=1, high=1, low=1, close=1, volume=1)
    forming_bar = SimpleNamespace(date=None, open=2, high=2, low=2, close=2, volume=1)
    handler = bot._make_bar_update_handler("AAA", contract, ctx)

    handler([closed_bar, forming_bar], True)

    assert len(added_bars) == 1
    assert calls == [(contract, ctx)]


def test_bar_update_handler_adds_the_closed_bar_not_the_new_one(tmp_path):
    # ib_async's historicalDataUpdate appends a bar and sets has_new_bar=True
    # the instant a new minute STARTS -- that new bar (bars[-1]) carries only
    # whatever's printed so far (often a single tick), while bars[-2] is the
    # bar that just fully closed. Feeding strategies bars[-1] here means
    # every "current bar" they see is really the barely-started next one,
    # permanently frozen at its first-tick snapshot (see main.py's
    # _make_bar_update_handler docstring). Confirmed against
    # ib_async/wrapper.py's historicalDataUpdate.
    bot = WarriorBot(make_config(tmp_path))
    added_bars = []
    ctx = SimpleNamespace(add_bar=lambda b: added_bars.append(b))
    contract = SimpleNamespace()
    bot._on_new_bar = lambda c, x: None
    closed_bar = SimpleNamespace(date="closed", open=1.0, high=1.5, low=0.9, close=1.4, volume=500)
    forming_bar = SimpleNamespace(date="forming", open=1.4, high=1.4, low=1.4, close=1.4, volume=1)
    handler = bot._make_bar_update_handler("AAA", contract, ctx)

    handler([closed_bar, forming_bar], True)

    assert len(added_bars) == 1
    assert added_bars[0].time == "closed"
    assert added_bars[0].volume == 500


def test_bar_update_handler_skips_first_bar_with_nothing_closed_yet(tmp_path):
    # A has_new_bar event with only one bar in the list means that lone bar
    # is itself the brand-new, still-forming one -- there is no bars[-2] yet
    # with a real, complete range to add. Adding bars[-1] here would just
    # reintroduce the bug for the very first bar of a subscription.
    bot = WarriorBot(make_config(tmp_path))
    added_bars = []
    ctx = SimpleNamespace(add_bar=lambda b: added_bars.append(b))
    contract = SimpleNamespace()
    calls = []
    bot._on_new_bar = lambda c, x: calls.append((c, x))
    fake_bar = SimpleNamespace(date=None, open=1, high=1, low=1, close=1, volume=1)
    handler = bot._make_bar_update_handler("AAA", contract, ctx)

    handler([fake_bar], True)

    assert added_bars == []
    assert calls == []


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


# -- position reconciliation watchdog: regression coverage for the
# 2026-09-16 NRXS incident (unprotected 850-share naked short for ~3h53m,
# closed only because the scheduled EOD flatten happened to still be ahead
# of it). See PositionReconciliationConfig (warrior_bot/config.py) for the
# full incident writeup.


class _FakePosition:
    def __init__(self, symbol, qty, exchange="NASDAQ"):
        self.contract = SimpleNamespace(symbol=symbol, exchange=exchange)
        self.position = qty


class _FakeOrder:
    def __init__(self, action, orderType, totalQuantity, remaining=None):
        self.action = action
        self.orderType = orderType
        self.totalQuantity = totalQuantity
        self.orderRef = ""


class _FakeTrade:
    def __init__(self, symbol, action, orderType, totalQuantity, remaining=None):
        self.contract = SimpleNamespace(symbol=symbol)
        self.order = _FakeOrder(action, orderType, totalQuantity)
        self.orderStatus = SimpleNamespace(remaining=remaining if remaining is not None else totalQuantity)


class _FakeFillEvent:
    """Unlike _FakeEventForMain, actually keeps listeners so a test can
    call .emit() to simulate the flatten order's fill arriving."""

    def __init__(self):
        self._listeners = []

    def __iadd__(self, listener):
        self._listeners.append(listener)
        return self

    def emit(self, *args) -> None:
        for listener in list(self._listeners):
            listener(*args)


def _fake_placed_trade(action="SELL", orderType="MKT", totalQuantity=770.0, orderId=555):
    """A placeOrder return value realistic enough for _journal_flatten_fill
    to wire onto -- has the order/status/fillEvent shape it reads."""
    order = SimpleNamespace(
        action=action, orderType=orderType, totalQuantity=totalQuantity, orderId=orderId, lmtPrice=None
    )
    trade = SimpleNamespace(orderStatus=SimpleNamespace(status="Submitted"), fillEvent=_FakeFillEvent())
    return trade, order


def _fake_lot(bot, symbol, strategy="gap_and_go", remaining_qty=100.0):
    """A real signals row (satisfies orders.signal_id's NOT NULL foreign
    key) plus a duck-typed ManagedPosition stand-in carrying just the two
    fields _journal_flatten_fill actually reads."""
    signal = Signal(
        symbol=symbol, strategy=strategy, side="BUY", entry_price=10.0, stop_price=9.0, target_price=12.0,
        ts=datetime.now(timezone.utc),
    )
    signal_id = bot.journal.record_signal(signal)
    # resize_task mirrors the real ManagedPosition field -- drop_symbol()/
    # clear() now cancel any armed stop-resize before untracking a lot (so a
    # resize can't fire after a flatten and resurrect a stop), and they read
    # it off every lot they drop.
    return SimpleNamespace(signal_id=signal_id, remaining_qty=remaining_qty, resize_task=None)


def test_reconciliation_drops_stale_local_tracking_when_broker_flat(tmp_path, monkeypatch):
    monkeypatch.setattr("warrior_bot.main.alert", lambda *a, **k: None)
    bot = WarriorBot(make_config(tmp_path))
    bot.position_manager._positions["GHOST"] = [SimpleNamespace(resize_task=None, parent_done=True)]
    bot.ib.positions = lambda: []
    bot.ib.openTrades = lambda: []

    bot._check_position_reconciliation()

    assert "GHOST" not in bot.position_manager.tracked_symbols()


def test_reconciliation_flattens_naked_short_regardless_of_resting_orders(tmp_path, monkeypatch):
    # A short is never an intended state for this long-only bot -- must be
    # flattened even if something happens to be resting on it.
    monkeypatch.setattr("warrior_bot.main.alert", lambda *a, **k: None)
    bot = WarriorBot(make_config(tmp_path))
    bot.ib.positions = lambda: [_FakePosition("NRXS", -850.0)]
    bot.ib.openTrades = lambda: [_FakeTrade("NRXS", "BUY", "STP LMT", 850.0)]
    placed = []

    def _place(contract, order):
        placed.append((contract, order))
        return _fake_placed_trade(action=order.action, totalQuantity=order.totalQuantity)[0]

    bot.ib.placeOrder = _place

    bot._check_position_reconciliation()

    assert len(placed) == 1
    contract, order = placed[0]
    assert contract.symbol == "NRXS"
    assert order.action == "BUY"
    assert order.totalQuantity == 850.0


def test_reconciliation_flattens_long_position_with_no_resting_stop(tmp_path, monkeypatch):
    monkeypatch.setattr("warrior_bot.main.alert", lambda *a, **k: None)
    bot = WarriorBot(make_config(tmp_path))
    bot.ib.positions = lambda: [_FakePosition("UCAR", 770.0)]
    bot.ib.openTrades = lambda: []  # nothing resting at all
    placed = []

    def _place(contract, order):
        placed.append((contract, order))
        return _fake_placed_trade(action=order.action, totalQuantity=order.totalQuantity)[0]

    bot.ib.placeOrder = _place

    bot._check_position_reconciliation()

    assert len(placed) == 1
    contract, order = placed[0]
    assert contract.symbol == "UCAR"
    assert order.action == "SELL"
    assert order.totalQuantity == 770.0


def test_reconciliation_leaves_protected_long_position_alone(tmp_path, monkeypatch):
    monkeypatch.setattr("warrior_bot.main.alert", lambda *a, **k: None)
    bot = WarriorBot(make_config(tmp_path))
    bot.ib.positions = lambda: [_FakePosition("UCAR", 770.0)]
    bot.ib.openTrades = lambda: [_FakeTrade("UCAR", "SELL", "STP LMT", 770.0)]
    placed = []

    def _place(contract, order):
        placed.append((contract, order))
        return _fake_placed_trade(action=order.action, totalQuantity=order.totalQuantity)[0]

    bot.ib.placeOrder = _place

    bot._check_position_reconciliation()

    assert placed == []


def test_reconciliation_treats_take_profit_limit_order_as_no_protection(tmp_path, monkeypatch):
    # A resting take-profit LMT sell doesn't cap downside -- only a
    # STP/STP LMT order actually protects a long position.
    monkeypatch.setattr("warrior_bot.main.alert", lambda *a, **k: None)
    bot = WarriorBot(make_config(tmp_path))
    bot.ib.positions = lambda: [_FakePosition("UCAR", 770.0)]
    bot.ib.openTrades = lambda: [_FakeTrade("UCAR", "SELL", "LMT", 770.0)]
    placed = []

    def _place(contract, order):
        placed.append((contract, order))
        return _fake_placed_trade(action=order.action, totalQuantity=order.totalQuantity)[0]

    bot.ib.placeOrder = _place

    bot._check_position_reconciliation()

    assert len(placed) == 1  # flagged as unprotected despite the resting LMT order


def test_reconciliation_sums_partial_stop_coverage_across_multiple_orders(tmp_path, monkeypatch):
    monkeypatch.setattr("warrior_bot.main.alert", lambda *a, **k: None)
    bot = WarriorBot(make_config(tmp_path))
    bot.ib.positions = lambda: [_FakePosition("UCAR", 770.0)]
    bot.ib.openTrades = lambda: [
        _FakeTrade("UCAR", "SELL", "STP LMT", 400.0),
        _FakeTrade("UCAR", "SELL", "STP", 370.0),  # 400+370=770, exactly covers it
    ]
    placed = []

    def _place(contract, order):
        placed.append((contract, order))
        return _fake_placed_trade(action=order.action, totalQuantity=order.totalQuantity)[0]

    bot.ib.placeOrder = _place

    bot._check_position_reconciliation()

    assert placed == []


def test_reconciliation_untracks_symbol_after_emergency_flatten(tmp_path, monkeypatch):
    monkeypatch.setattr("warrior_bot.main.alert", lambda *a, **k: None)
    bot = WarriorBot(make_config(tmp_path))
    bot.position_manager._positions["UCAR"] = [_fake_lot(bot, "UCAR", remaining_qty=770.0)]
    bot.ib.positions = lambda: [_FakePosition("UCAR", 770.0)]
    bot.ib.openTrades = lambda: []
    bot.ib.placeOrder = lambda contract, order: _fake_placed_trade()[0]

    bot._check_position_reconciliation()

    assert "UCAR" not in bot.position_manager.tracked_symbols()


# -- _journal_flatten_fill: regression coverage for the 2026-09-23 finding
# that emergency/EOD flatten orders were never journaled at all (no
# orders/fills row -- ib.placeOrder() called directly with no listener),
# so any position force-closed by the reconciliation watchdog or a routine
# EOD flatten showed up in dashboard_report.py as "still open, $0
# realized" forever, understating the true damage on days like 2026-09-21.


def _fetch_fills_for_signal(bot, signal_id):
    columns = ["role", "action", "order_qty", "fill_qty", "fill_price", "commission"]
    rows = bot.journal.conn.execute(
        "SELECT o.role, o.action, o.qty AS order_qty, f.fill_qty, f.fill_price, f.commission "
        "FROM orders o JOIN fills f ON f.order_id = o.id WHERE o.signal_id = ?",
        (signal_id,),
    ).fetchall()
    return [dict(zip(columns, row)) for row in rows]


def test_journal_flatten_fill_records_order_and_fill_for_single_lot(tmp_path, monkeypatch):
    monkeypatch.setattr("warrior_bot.main.alert", lambda *a, **k: None)
    bot = WarriorBot(make_config(tmp_path))
    lot = _fake_lot(bot, "UCAR", remaining_qty=770.0)
    bot.position_manager._positions["UCAR"] = [lot]
    trade, order = _fake_placed_trade(action="SELL", totalQuantity=770.0)

    bot._journal_flatten_fill("UCAR", trade, order)
    trade.fillEvent.emit(trade, SimpleNamespace(execution=SimpleNamespace(shares=770.0, price=4.5), commissionReport=None))

    fills = _fetch_fills_for_signal(bot, lot.signal_id)
    assert len(fills) == 1
    assert fills[0]["role"] == "emergency_flatten"
    assert fills[0]["action"] == "SELL"
    assert fills[0]["fill_qty"] == 770.0
    assert fills[0]["fill_price"] == 4.5


def test_journal_flatten_fill_splits_proportionally_across_two_lots(tmp_path, monkeypatch):
    monkeypatch.setattr("warrior_bot.main.alert", lambda *a, **k: None)
    bot = WarriorBot(make_config(tmp_path))
    lot_a = _fake_lot(bot, "UCAR", remaining_qty=300.0)  # 30% of the combined position
    lot_b = _fake_lot(bot, "UCAR", remaining_qty=700.0)  # 70%
    bot.position_manager._positions["UCAR"] = [lot_a, lot_b]
    trade, order = _fake_placed_trade(action="SELL", totalQuantity=1000.0)

    bot._journal_flatten_fill("UCAR", trade, order)
    trade.fillEvent.emit(trade, SimpleNamespace(execution=SimpleNamespace(shares=1000.0, price=2.0), commissionReport=None))

    fills_a = _fetch_fills_for_signal(bot, lot_a.signal_id)
    fills_b = _fetch_fills_for_signal(bot, lot_b.signal_id)
    assert fills_a[0]["fill_qty"] == 300.0
    assert fills_b[0]["fill_qty"] == 700.0
    # both lots' order rows reflect the same real order size, split the same way
    assert fills_a[0]["order_qty"] == 300.0
    assert fills_b[0]["order_qty"] == 700.0


def test_journal_flatten_fill_splits_a_shared_execution_across_lots(tmp_path, monkeypatch):
    # One IBKR execution, two lots -> two rows. fills.exec_id is UNIQUE, so
    # each part needs a distinct-but-deterministic id or INSERT OR IGNORE
    # would silently drop the second (see _split_exec_id).
    #
    # Commission is no longer asserted here: ib_async emits fillEvent with an
    # empty CommissionReport and sends the real numbers separately, so
    # reading it at fill time always recorded 0.0. It now arrives via
    # OrderManager._on_commission_report.
    monkeypatch.setattr("warrior_bot.main.alert", lambda *a, **k: None)
    bot = WarriorBot(make_config(tmp_path))
    lot_a = _fake_lot(bot, "UCAR", remaining_qty=250.0)  # 25%
    lot_b = _fake_lot(bot, "UCAR", remaining_qty=750.0)  # 75%
    bot.position_manager._positions["UCAR"] = [lot_a, lot_b]
    trade, order = _fake_placed_trade(action="SELL", totalQuantity=1000.0)

    bot._journal_flatten_fill("UCAR", trade, order)
    trade.fillEvent.emit(
        trade,
        SimpleNamespace(
            execution=SimpleNamespace(shares=1000.0, price=2.0, execId="exec-shared", time=None),
            commissionReport=None,
        ),
    )

    fills_a = _fetch_fills_for_signal(bot, lot_a.signal_id)
    fills_b = _fetch_fills_for_signal(bot, lot_b.signal_id)
    assert fills_a[0]["fill_qty"] == 250.0
    assert fills_b[0]["fill_qty"] == 750.0
    exec_ids = {
        r[0]
        for r in bot.journal.conn.execute("SELECT exec_id FROM fills WHERE exec_id IS NOT NULL")
    }
    assert len(exec_ids) == 2  # both parts survived the UNIQUE index
    assert all(e.startswith("exec-shared#") for e in exec_ids)


def test_unattributable_flatten_is_journaled_against_the_symbol(tmp_path, monkeypatch):
    # THE Round 2 fix. This used to `return` without writing anything,
    # because orders.signal_id was NOT NULL and there was no signal to point
    # at -- the single biggest reason only ~45% of traded notional had a
    # journaled exit, and why there were 2 emergency_flatten rows in six
    # weeks. The shares genuinely left the account; the row belongs in the
    # journal with signal_id NULL and the symbol carrying attribution.
    monkeypatch.setattr("warrior_bot.main.alert", lambda *a, **k: None)
    bot = WarriorBot(make_config(tmp_path))
    trade, order = _fake_placed_trade(action="SELL", totalQuantity=500.0)

    bot._journal_flatten_fill("GHOST", trade, order)
    trade.fillEvent.emit(
        trade,
        SimpleNamespace(
            execution=SimpleNamespace(shares=500.0, price=3.0, execId="exec-ghost", time=None),
            commissionReport=None,
        ),
    )

    rows = bot.journal.conn.execute(
        "SELECT signal_id, symbol, role FROM orders WHERE symbol = 'GHOST'"
    ).fetchall()
    assert rows == [(None, "GHOST", "emergency_flatten")]
    fills = bot.journal.conn.execute(
        "SELECT f.fill_qty, f.fill_price FROM fills f JOIN orders o ON o.id = f.order_id "
        "WHERE o.symbol = 'GHOST'"
    ).fetchall()
    assert fills == [(500.0, 3.0)]


def test_journal_flatten_fill_wired_through_emergency_flatten_end_to_end(tmp_path, monkeypatch):
    # Exercises the real call path: _check_position_reconciliation ->
    # _emergency_flatten_symbol -> flatten_position -> on_order_placed.
    monkeypatch.setattr("warrior_bot.main.alert", lambda *a, **k: None)
    bot = WarriorBot(make_config(tmp_path))
    lot = _fake_lot(bot, "UCAR", remaining_qty=770.0)
    bot.position_manager._positions["UCAR"] = [lot]
    bot.ib.positions = lambda: [_FakePosition("UCAR", 770.0)]
    bot.ib.openTrades = lambda: []
    placed_trade, _ = _fake_placed_trade(action="SELL", totalQuantity=770.0)
    bot.ib.placeOrder = lambda contract, order: placed_trade

    bot._check_position_reconciliation()
    placed_trade.fillEvent.emit(
        placed_trade, SimpleNamespace(execution=SimpleNamespace(shares=770.0, price=4.5), commissionReport=None)
    )

    fills = _fetch_fills_for_signal(bot, lot.signal_id)
    assert len(fills) == 1
    assert fills[0]["role"] == "emergency_flatten"


def test_journal_flatten_fill_wired_through_trigger_flatten_end_to_end(tmp_path, monkeypatch):
    # Exercises the routine EOD/loss-limit path: _trigger_flatten ->
    # panic_stop -> flatten_all_positions -> flatten_position -> on_order_placed.
    monkeypatch.setattr("warrior_bot.main.alert", lambda *a, **k: None)
    monkeypatch.setattr("warrior_bot.utils.panic.alert", lambda *a, **k: None)
    bot = WarriorBot(make_config(tmp_path))
    lot = _fake_lot(bot, "UCAR", remaining_qty=770.0)
    bot.position_manager._positions["UCAR"] = [lot]
    bot.ib.positions = lambda: [_FakePosition("UCAR", 770.0)]
    bot.ib.openTrades = lambda: []
    placed_trade, _ = _fake_placed_trade(action="SELL", totalQuantity=770.0)
    bot.ib.placeOrder = lambda contract, order: placed_trade
    bot.ib.reqGlobalCancel = lambda: None

    bot._trigger_flatten("eod_flatten")
    placed_trade.fillEvent.emit(
        placed_trade, SimpleNamespace(execution=SimpleNamespace(shares=770.0, price=4.5), commissionReport=None)
    )

    fills = _fetch_fills_for_signal(bot, lot.signal_id)
    assert len(fills) == 1
    assert fills[0]["role"] == "emergency_flatten"


class _RankCtx:
    def __init__(self, rank):
        self.scanner_rank = rank


def test_scan_refreshes_rank_of_already_tracked_symbols(tmp_path):
    # The whole point of the reserved top-tier slot is the day's most
    # obvious name -- which is rarely the name that was most obvious when
    # it first got onboarded.
    bot = WarriorBot(make_config(tmp_path))
    bot.contexts = {"AAA": _RankCtx(18), "BBB": _RankCtx(1)}

    for rank, symbol in enumerate(["BBB", "AAA"], start=1):
        bot.contexts[symbol].scanner_rank = rank
    bot._demote_symbols_absent_from_scan({"BBB", "AAA"})

    assert bot.contexts["BBB"].scanner_rank == 1
    assert bot.contexts["AAA"].scanner_rank == 2


def test_symbols_dropping_out_of_the_scan_lose_their_rank(tmp_path):
    bot = WarriorBot(make_config(tmp_path))
    bot.contexts = {"AAA": _RankCtx(1), "BBB": _RankCtx(2)}

    bot._demote_symbols_absent_from_scan({"AAA"})

    assert bot.contexts["AAA"].scanner_rank == 1
    assert bot.contexts["BBB"].scanner_rank is None


# -- _eligible_for_new_signals: 2026-09-26, Ross trades only the top 2-3 most
# obvious gainers each morning -- until now every onboarded scanner
# candidate was equally eligible for every strategy regardless of rank.


def test_eligible_for_new_signals_true_within_rank_cutoff(tmp_path):
    bot = WarriorBot(make_config(tmp_path, scanner=ScannerConfig(max_eligible_rank=3)))

    assert bot._eligible_for_new_signals(_RankCtx(1)) is True
    assert bot._eligible_for_new_signals(_RankCtx(3)) is True


def test_eligible_for_new_signals_false_outside_rank_cutoff(tmp_path):
    bot = WarriorBot(make_config(tmp_path, scanner=ScannerConfig(max_eligible_rank=3)))

    assert bot._eligible_for_new_signals(_RankCtx(4)) is False


def test_eligible_for_new_signals_false_when_rank_missing(tmp_path):
    # A symbol with no current rank (never ranked, or dropped out of the
    # scanner's top-N -- see _demote_symbols_absent_from_scan) is not
    # eligible for a cutoff reserved for the day's most obvious names.
    bot = WarriorBot(make_config(tmp_path, scanner=ScannerConfig(max_eligible_rank=3)))

    assert bot._eligible_for_new_signals(_RankCtx(None)) is False


def test_eligible_for_new_signals_always_true_when_cutoff_disabled(tmp_path):
    bot = WarriorBot(make_config(tmp_path, scanner=ScannerConfig(max_eligible_rank=None)))

    assert bot._eligible_for_new_signals(_RankCtx(None)) is True
    assert bot._eligible_for_new_signals(_RankCtx(50)) is True


def test_on_new_bar_skips_strategy_evaluation_for_ineligible_rank(tmp_path):
    bot = WarriorBot(make_config(tmp_path, scanner=ScannerConfig(max_eligible_rank=3)))
    ctx = _RankCtx(5)
    ctx.symbol = "AAA"
    bot.position_manager.on_bar = lambda c: None
    called = []

    class _FakeStrategy:
        name = "fake"

        def evaluate(self, c, now):
            called.append(c)
            return None

    bot.strategies = [_FakeStrategy()]

    bot._on_new_bar(SimpleNamespace(), ctx)

    assert called == []  # rank 5 is outside the top-3 cutoff -- never even evaluated


def test_on_new_bar_still_manages_existing_position_for_ineligible_rank(tmp_path):
    # A symbol whose rank slips outside the cutoff must keep having its
    # already-open position managed normally -- only NEW signal generation
    # stops.
    bot = WarriorBot(make_config(tmp_path, scanner=ScannerConfig(max_eligible_rank=3)))
    ctx = _RankCtx(5)
    ctx.symbol = "AAA"
    on_bar_calls = []
    bot.position_manager.on_bar = lambda c: on_bar_calls.append(c)
    bot.strategies = []

    bot._on_new_bar(SimpleNamespace(), ctx)

    assert on_bar_calls == [ctx]


def test_loss_limit_flatten_does_not_suppress_later_eod_sweep(tmp_path, monkeypatch):
    # Confirmed live, 2026-09-21: a single shared _flattened_today flag let
    # an early daily-loss-limit flatten permanently block the 15:55 EOD
    # sweep for the rest of that day. should_flatten_for_loss_limit
    # re-derives from live P&L on every check rather than latching, so
    # realized P&L can recover and new entries resume after the early
    # flatten -- anything opened in that gap had nothing left to close it.
    triggered = []
    monkeypatch.setattr("warrior_bot.main.panic_stop", lambda *a, **k: triggered.append("flatten"))
    bot = WarriorBot(make_config(tmp_path))
    bot.account_state.snapshot = lambda: _fake_snapshot()

    # Frozen well before the 15:55 ET EOD cutoff -- otherwise this test is
    # only correct if it happens to run before 15:55 ET wall-clock itself,
    # which made it flaky (real failure seen running the suite at 16:37 ET:
    # the very first _check_flatten_triggers() call took the EOD branch
    # before ever reaching the loss-limit branch this test means to
    # exercise "mid-morning").
    class _MorningDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 21, 14, 0, 0, tzinfo=timezone.utc)  # 10:00 ET

    monkeypatch.setattr("warrior_bot.main.datetime", _MorningDatetime)

    # An early loss-limit flatten fires mid-morning.
    bot.risk_manager.should_flatten_for_loss_limit = lambda snapshot: True
    bot._check_flatten_triggers()
    assert bot._loss_limit_flatten_fired is True
    assert bot._eod_flatten_fired is False
    assert len(triggered) == 1

    # P&L recovers; no further loss-limit trigger, no EOD time yet -- a
    # quiet tick should do nothing.
    bot.risk_manager.should_flatten_for_loss_limit = lambda snapshot: False
    bot._check_flatten_triggers()
    assert len(triggered) == 1

    # 15:55 ET arrives -- the EOD sweep must still fire, even though a
    # flatten already ran once today for a different reason.
    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 21, 19, 56, 0, tzinfo=timezone.utc)  # 15:56 ET

    monkeypatch.setattr("warrior_bot.main.datetime", _FrozenDatetime)
    bot._check_flatten_triggers()
    assert bot._eod_flatten_fired is True
    assert len(triggered) == 2


def test_restore_or_start_daily_risk_state_starts_fresh_when_nothing_persisted(tmp_path):
    bot = WarriorBot(make_config(tmp_path))
    bot.account_state.snapshot = lambda: _fake_snapshot()
    bot.account_state.snapshot = lambda: AccountSnapshot(
        net_liquidation=45_000.0, available_funds=45_000.0, buying_power=45_000.0,
        open_positions_count=0, daily_realized_pnl=0.0,
    )
    bot._trading_day = _today_et()

    bot._restore_or_start_daily_risk_state()

    assert bot.risk_manager.start_of_day_equity == 45_000.0
    assert bot.risk_manager.loss_limit_halted_today is False
    # Persisted immediately, not just held in memory -- a restart seconds
    # later must see this same baseline.
    persisted = bot.journal.load_daily_risk_state(_today_et().isoformat())
    assert persisted == {"start_of_day_equity": 45_000.0, "loss_limit_halted": False}


def test_restore_or_start_daily_risk_state_restores_an_active_halt(tmp_path):
    # The live incident this whole feature fixes: a same-day restart must
    # not un-halt an already-breached day.
    bot = WarriorBot(make_config(tmp_path))
    bot._trading_day = _today_et()
    bot.journal.save_daily_risk_state(
        _today_et().isoformat(), start_of_day_equity=45_000.0, loss_limit_halted=True
    )
    bot.account_state.snapshot = lambda: AccountSnapshot(
        net_liquidation=44_000.0, available_funds=44_000.0, buying_power=44_000.0,
        open_positions_count=0, daily_realized_pnl=0.0,  # recovered back to "not breached" -- must not matter
    )

    bot._restore_or_start_daily_risk_state()

    assert bot.risk_manager.start_of_day_equity == 45_000.0  # the ORIGINAL baseline, not current equity
    assert bot.risk_manager.loss_limit_halted_today is True
    assert not bot.risk_manager.evaluate(make_signal()).accepted


def test_check_flatten_triggers_persists_risk_state_every_tick(tmp_path):
    bot = WarriorBot(make_config(tmp_path))
    bot._trading_day = _today_et()
    bot.risk_manager.mark_start_of_day(45_000.0)
    bot.account_state.snapshot = lambda: _fake_snapshot()
    bot.risk_manager.should_flatten_for_loss_limit = lambda snapshot: False

    class _MorningDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 23, 14, 0, 0, tzinfo=timezone.utc)  # 10:00 ET, before EOD cutoff

    import warrior_bot.main as main_module
    original_datetime = main_module.datetime
    main_module.datetime = _MorningDatetime
    try:
        assert bot.journal.load_daily_risk_state(_today_et().isoformat()) is None
        bot._check_flatten_triggers()
    finally:
        main_module.datetime = original_datetime

    assert bot.journal.load_daily_risk_state(_today_et().isoformat()) == {
        "start_of_day_equity": 45_000.0,
        "loss_limit_halted": False,
    }


def test_new_trading_day_persists_fresh_state_under_todays_date_not_yesterdays(tmp_path):
    # Regression guard for the ordering bug: reset_daily_state() persists
    # keyed by self._trading_day, which must already be TODAY's date by
    # the time it runs, not yesterday's (the date _check_new_trading_day
    # was about to replace).
    bot = WarriorBot(make_config(tmp_path))
    bot.account_state.snapshot = lambda: _fake_snapshot()
    yesterday = _yesterday_et()
    bot._trading_day = yesterday
    bot.journal.save_daily_risk_state(yesterday.isoformat(), start_of_day_equity=40_000.0, loss_limit_halted=True)

    bot._check_new_trading_day()

    assert bot._trading_day == _today_et()
    today_state = bot.journal.load_daily_risk_state(_today_et().isoformat())
    assert today_state is not None
    assert today_state["loss_limit_halted"] is False  # fresh day, not carried over from yesterday
    # Yesterday's own row is untouched, not overwritten by today's reset.
    assert bot.journal.load_daily_risk_state(yesterday.isoformat()) == {
        "start_of_day_equity": 40_000.0,
        "loss_limit_halted": True,
    }


# -- 2026-09-28 execution-layer audit: reconciliation and reconnect fixes --


def test_reconciliation_keeps_a_symbol_whose_entry_is_still_working(tmp_path, monkeypatch):
    # track() registers a lot the instant the bracket is submitted, with
    # remaining_qty=0, and entry_fill_timeout_seconds is 300s -- so IBKR
    # correctly reports no position for a working limit entry. Dropping it
    # left the lot with a stop but no breakeven/trailing/reversal (on_bar
    # can't see it), outside the position caps, and invisible to
    # cancel_stale_entries.
    monkeypatch.setattr("warrior_bot.main.alert", lambda *a, **k: None)
    bot = WarriorBot(make_config(tmp_path))
    bot.position_manager._positions["SLOW"] = [
        SimpleNamespace(resize_task=None, parent_done=False)  # entry still working
    ]
    bot.ib.positions = lambda: []
    bot.ib.openTrades = lambda: []

    bot._check_position_reconciliation()

    assert "SLOW" in bot.position_manager.tracked_symbols()


def test_reconciliation_still_drops_a_symbol_whose_entry_is_done(tmp_path, monkeypatch):
    monkeypatch.setattr("warrior_bot.main.alert", lambda *a, **k: None)
    bot = WarriorBot(make_config(tmp_path))
    bot.position_manager._positions["DONE"] = [SimpleNamespace(resize_task=None, parent_done=True)]
    bot.ib.positions = lambda: []
    bot.ib.openTrades = lambda: []

    bot._check_position_reconciliation()

    assert "DONE" not in bot.position_manager.tracked_symbols()


def _stranded_stop_trade(symbol="GAPR", trigger=10.0, qty=100.0):
    trade = _FakeTrade(symbol, "SELL", "STP LMT", qty)
    trade.order.auxPrice = trigger
    trade.orderStatus.filled = 0.0
    return trade


def test_triggered_but_unfilled_stop_limit_no_longer_counts_as_protection(tmp_path, monkeypatch):
    # Every stop is a STP LMT with the limit only 0.5% under the trigger.
    # In a gap-down the stop triggers and the limit is left behind; the
    # order keeps reporting full `remaining`, so the watchdog scored the
    # position 100% covered while it was actually naked.
    monkeypatch.setattr("warrior_bot.main.alert", lambda *a, **k: None)
    bot = WarriorBot(make_config(tmp_path))
    bot.contexts["GAPR"] = SimpleNamespace(symbol="GAPR", last_price=8.0)  # gapped well under the 10.0 trigger
    bot.ib.positions = lambda: [_FakePosition("GAPR", 100.0)]
    bot.ib.openTrades = lambda: [_stranded_stop_trade()]
    placed = []

    def _place(contract, order):
        placed.append((contract, order))
        return _fake_placed_trade(action=order.action, totalQuantity=order.totalQuantity)[0]

    bot.ib.placeOrder = _place

    bot._check_position_reconciliation()

    assert len(placed) == 1  # flattened, not treated as covered
    assert placed[0][1].action == "SELL"


def test_stop_limit_sitting_near_its_trigger_still_counts_as_protection(tmp_path, monkeypatch):
    monkeypatch.setattr("warrior_bot.main.alert", lambda *a, **k: None)
    bot = WarriorBot(make_config(tmp_path))
    bot.contexts["CALM"] = SimpleNamespace(symbol="CALM", last_price=10.05)  # trading above its trigger
    bot.ib.positions = lambda: [_FakePosition("CALM", 100.0)]
    bot.ib.openTrades = lambda: [_stranded_stop_trade(symbol="CALM")]
    placed = []

    def _place(contract, order):
        placed.append((contract, order))
        return _fake_placed_trade(action=order.action, totalQuantity=order.totalQuantity)[0]

    bot.ib.placeOrder = _place

    bot._check_position_reconciliation()

    assert placed == []


def test_stranded_check_is_skipped_without_a_price_reference(tmp_path, monkeypatch):
    # No bar feed for the symbol -- fall back to the old behaviour rather
    # than flattening on a guess.
    monkeypatch.setattr("warrior_bot.main.alert", lambda *a, **k: None)
    bot = WarriorBot(make_config(tmp_path))
    bot.ib.positions = lambda: [_FakePosition("NOCTX", 100.0)]
    bot.ib.openTrades = lambda: [_stranded_stop_trade(symbol="NOCTX")]
    placed = []

    def _place(contract, order):
        placed.append((contract, order))
        return _fake_placed_trade(action=order.action, totalQuantity=order.totalQuantity)[0]

    bot.ib.placeOrder = _place

    bot._check_position_reconciliation()

    assert placed == []


def test_reconnect_queues_held_symbols_for_re_onboarding(tmp_path, monkeypatch):
    # _scan_loop only onboards names in the current top-N, so a held
    # position that dropped out of the scan never got bars again -- no
    # breakeven, no trailing, no reversal exit for the rest of the day.
    monkeypatch.setattr("warrior_bot.main.alert", lambda *a, **k: None)
    bot = WarriorBot(make_config(tmp_path))
    bot.contexts["AAA"] = object()
    bot.position_manager._positions["HELD"] = [SimpleNamespace(resize_task=None, parent_done=True)]
    bot.position_manager.resync_after_reconnect = lambda ib: {7}
    bot.order_manager.resync_open_orders = lambda **kwargs: None

    bot._on_connected()

    assert bot._resubscribe_after_reconnect == {"HELD"}


def test_reconnect_alerts_when_nothing_resynced_but_positions_are_held(tmp_path, monkeypatch):
    alerts = []
    monkeypatch.setattr("warrior_bot.main.alert", lambda message, channel=None: alerts.append((message, channel)))
    bot = WarriorBot(make_config(tmp_path))
    bot.contexts["AAA"] = object()
    bot.position_manager._positions["HELD"] = [SimpleNamespace(resize_task=None, parent_done=True)]
    bot.position_manager.resync_after_reconnect = lambda ib: set()  # nothing found at the broker
    bot.order_manager.resync_open_orders = lambda **kwargs: None

    bot._on_connected()

    assert any("resynced 0 orders" in message for message, _ in alerts)


# -- 2026-09-28 audit: scanner-refusal detection and disconnect visibility --


def test_scanner_refusal_does_not_demote_ranks(tmp_path, monkeypatch):
    # THE key interaction. An empty scan clears every symbol's rank, and
    # since the 2026-09-26 eligibility gate a cleared rank makes a symbol
    # ineligible for every strategy -- so a refused scan silently took the
    # entire strategy layer offline rather than just pausing discovery.
    monkeypatch.setattr("warrior_bot.main.alert", lambda *a, **k: None)
    bot = WarriorBot(make_config(tmp_path))
    bot.contexts = {"AAA": _RankCtx(1), "BBB": _RankCtx(2)}

    asyncio.run(bot._handle_scanner_refusal(RuntimeError("code 322")))

    assert bot.contexts["AAA"].scanner_rank == 1  # untouched
    assert bot.contexts["BBB"].scanner_rank == 2
    assert bot._consecutive_scanner_refusals == 1


def test_scanner_refusal_alerts_on_the_first_failure(tmp_path, monkeypatch):
    alerts = []
    monkeypatch.setattr("warrior_bot.main.alert", lambda message, channel=None: alerts.append(message))
    bot = WarriorBot(make_config(tmp_path))

    asyncio.run(bot._handle_scanner_refusal(RuntimeError("code 322")))

    assert any("REFUSED" in message for message in alerts)


def test_repeated_scanner_refusals_force_a_reconnect(tmp_path, monkeypatch):
    # Scanner subscription slots leak per API connection, so only a
    # reconnect actually releases them -- confirmed by the manual restart
    # that recovered the 2026-09-28 outage.
    monkeypatch.setattr("warrior_bot.main.alert", lambda *a, **k: None)
    bot = WarriorBot(make_config(tmp_path))
    disconnects = []
    bot.ib.disconnect = lambda: disconnects.append(True)

    for _ in range(bot.SCANNER_REFUSAL_RECONNECT_THRESHOLD):
        asyncio.run(bot._handle_scanner_refusal(RuntimeError("code 322")))

    assert len(disconnects) == 1


def test_scanner_recovery_resets_the_counter_and_alerts(tmp_path, monkeypatch):
    alerts = []
    monkeypatch.setattr("warrior_bot.main.alert", lambda message, channel=None: alerts.append(message))
    bot = WarriorBot(make_config(tmp_path))
    asyncio.run(bot._handle_scanner_refusal(RuntimeError("code 322")))

    bot._note_scan_succeeded()

    assert bot._consecutive_scanner_refusals == 0
    assert bot._last_successful_scan_at is not None
    assert any("recovered" in message for message in alerts)


def test_disconnect_is_logged_once_then_throttled(tmp_path, monkeypatch, caplog):
    # The loops used to `sleep(5); continue` in silence -- 5h20m of outage
    # produced 3-15 log lines an hour and no alert at all.
    monkeypatch.setattr("warrior_bot.main.alert", lambda *a, **k: None)
    bot = WarriorBot(make_config(tmp_path))
    bot.ib.isConnected = lambda: False

    with caplog.at_level("WARNING"):
        assert asyncio.run(bot._await_connection("scan_loop")) is False
        assert asyncio.run(bot._await_connection("risk_loop")) is False  # same outage, throttled

    messages = [r.message for r in caplog.records]
    assert sum("not connected" in m for m in messages) == 1
    assert bot._disconnected_since is not None


def test_reconnection_is_reported_with_its_duration(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr("warrior_bot.main.alert", lambda *a, **k: None)
    bot = WarriorBot(make_config(tmp_path))
    bot.ib.isConnected = lambda: False
    asyncio.run(bot._await_connection("scan_loop"))

    bot.ib.isConnected = lambda: True
    with caplog.at_level("WARNING"):
        assert asyncio.run(bot._await_connection("scan_loop")) is True

    assert any("connection restored" in r.message for r in caplog.records)
    assert bot._disconnected_since is None


def test_long_disconnect_alerts_during_an_active_session(tmp_path, monkeypatch):
    alerts = []
    monkeypatch.setattr("warrior_bot.main.alert", lambda message, channel=None: alerts.append(message))
    monkeypatch.setattr("warrior_bot.main.is_active_session", lambda: True)
    bot = WarriorBot(make_config(tmp_path))
    bot.ib.isConnected = lambda: False
    asyncio.run(bot._await_connection("scan_loop"))
    # Backdate the outage past the alert threshold.
    bot._disconnected_since = datetime.now(timezone.utc) - timedelta(seconds=300)

    asyncio.run(bot._await_connection("scan_loop"))

    assert any("disconnected for" in message for message in alerts)
    # One-shot: a second pass must not re-alert.
    alerts.clear()
    asyncio.run(bot._await_connection("scan_loop"))
    assert alerts == []


# -- 2026-09-28 audit: "alive but idle" detection --


def _heartbeat_rows(bot):
    cols = ["ts", "connected", "symbols_subscribed", "bars_received_last_min", "signals_today",
            "open_positions", "breadth", "scanner_refusals", "seconds_since_scan"]
    rows = bot.journal.conn.execute(
        "SELECT ts, connected, symbols_subscribed, bars_received_last_min, signals_today, "
        "open_positions, breadth, scanner_refusals, seconds_since_scan FROM bot_heartbeat ORDER BY id"
    ).fetchall()
    return [dict(zip(cols, r)) for r in rows]


def test_heartbeat_records_liveness(tmp_path, monkeypatch):
    # "Was the bot actually working at 10:15?" was previously answerable
    # only by log archaeology, and only while the tail still reached back.
    monkeypatch.setattr("warrior_bot.main.alert", lambda *a, **k: None)
    monkeypatch.setattr("warrior_bot.main.is_active_session", lambda: True)
    bot = WarriorBot(make_config(tmp_path))
    bot.ib.isConnected = lambda: True
    _seed_symbol(bot, "AAA")
    bot._signals_today = 3

    bot._record_heartbeat()

    rows = _heartbeat_rows(bot)
    assert len(rows) == 1
    assert rows[0]["connected"] == 1
    assert rows[0]["symbols_subscribed"] == 1
    assert rows[0]["bars_received_last_min"] == 1
    assert rows[0]["signals_today"] == 3


def test_idle_alert_fires_when_connected_but_receiving_no_bars(tmp_path, monkeypatch):
    # The exact 2026-09-28 shape: connected, subscribed, and silently
    # producing nothing because the scanner was dead.
    alerts = []
    monkeypatch.setattr("warrior_bot.main.alert", lambda message, channel=None: alerts.append(message))
    monkeypatch.setattr("warrior_bot.main.is_active_session", lambda: True)
    bot = WarriorBot(make_config(tmp_path))
    bot.ib.isConnected = lambda: True
    _seed_symbol(bot, "AAA")
    bot._last_bar_at["AAA"] = datetime.now(timezone.utc) - timedelta(hours=2)  # stale
    bot._last_productive_at = datetime.now(timezone.utc) - timedelta(seconds=1200)

    bot._record_heartbeat()

    assert any("up but blind" in message for message in alerts)
    # One-shot until productivity resumes.
    alerts.clear()
    bot._record_heartbeat()
    assert alerts == []


def test_idle_alert_clears_once_bars_resume(tmp_path, monkeypatch):
    monkeypatch.setattr("warrior_bot.main.alert", lambda *a, **k: None)
    monkeypatch.setattr("warrior_bot.main.is_active_session", lambda: True)
    bot = WarriorBot(make_config(tmp_path))
    bot.ib.isConnected = lambda: True
    _seed_symbol(bot, "AAA")
    bot._last_bar_at["AAA"] = datetime.now(timezone.utc) - timedelta(hours=2)
    bot._last_productive_at = datetime.now(timezone.utc) - timedelta(seconds=1200)
    bot._record_heartbeat()
    assert bot._idle_alert_sent is True

    bot._last_bar_at["AAA"] = datetime.now(timezone.utc)  # data is flowing again
    bot._record_heartbeat()

    assert bot._idle_alert_sent is False


def test_no_idle_alert_outside_an_active_session(tmp_path, monkeypatch):
    alerts = []
    monkeypatch.setattr("warrior_bot.main.alert", lambda message, channel=None: alerts.append(message))
    monkeypatch.setattr("warrior_bot.main.is_active_session", lambda: False)  # overnight
    bot = WarriorBot(make_config(tmp_path))
    bot.ib.isConnected = lambda: True
    _seed_symbol(bot, "AAA")
    bot._last_bar_at["AAA"] = datetime.now(timezone.utc) - timedelta(hours=8)
    bot._last_productive_at = datetime.now(timezone.utc) - timedelta(hours=8)

    bot._record_heartbeat()

    assert alerts == []


def test_no_idle_alert_while_disconnected(tmp_path, monkeypatch):
    # A disconnect has its own alert path; this one must not double up.
    alerts = []
    monkeypatch.setattr("warrior_bot.main.alert", lambda message, channel=None: alerts.append(message))
    monkeypatch.setattr("warrior_bot.main.is_active_session", lambda: True)
    bot = WarriorBot(make_config(tmp_path))
    bot.ib.isConnected = lambda: False
    _seed_symbol(bot, "AAA")
    bot._last_productive_at = datetime.now(timezone.utc) - timedelta(hours=1)

    bot._record_heartbeat()

    assert alerts == []
    assert _heartbeat_rows(bot)[0]["connected"] == 0  # still recorded


# -- Round 2: equity curve --


def test_equity_is_snapshotted_from_the_risk_loop(tmp_path, monkeypatch):
    # account_snapshots was written exactly once, in start() -- 46 rows in
    # six weeks, one per restart. No equity curve meant no drawdown, no
    # intraday excursion, and no independent check on fill-derived P&L.
    monkeypatch.setattr("warrior_bot.main.alert", lambda *a, **k: None)
    bot = WarriorBot(make_config(tmp_path))
    bot.account_state.snapshot = lambda: _fake_snapshot()

    bot._maybe_record_equity()

    rows = bot.journal.conn.execute("SELECT count(*) FROM account_snapshots").fetchone()[0]
    assert rows == 1


def test_equity_snapshot_is_throttled(tmp_path, monkeypatch):
    monkeypatch.setattr("warrior_bot.main.alert", lambda *a, **k: None)
    bot = WarriorBot(make_config(tmp_path))
    bot.account_state.snapshot = lambda: _fake_snapshot()

    bot._maybe_record_equity()
    bot._maybe_record_equity()  # immediately again -- must not double-write

    assert bot.journal.conn.execute("SELECT count(*) FROM account_snapshots").fetchone()[0] == 1

    bot._last_equity_snapshot_at = datetime.now(timezone.utc) - timedelta(seconds=120)
    bot._maybe_record_equity()
    assert bot.journal.conn.execute("SELECT count(*) FROM account_snapshots").fetchone()[0] == 2


def test_equity_snapshot_skipped_when_account_values_are_missing(tmp_path, monkeypatch):
    # A post-reconnect empty accountValues must not be recorded as a real
    # equity reading -- that is the same class of bug as the zero-equity
    # halt fixed in Round 1.
    monkeypatch.setattr("warrior_bot.main.alert", lambda *a, **k: None)
    bot = WarriorBot(make_config(tmp_path))
    bot.account_state.snapshot = lambda: AccountSnapshot(
        net_liquidation=None, available_funds=None, buying_power=None,
        open_positions_count=0, daily_realized_pnl=0.0,
    )

    bot._maybe_record_equity()

    assert bot.journal.conn.execute("SELECT count(*) FROM account_snapshots").fetchone()[0] == 0
