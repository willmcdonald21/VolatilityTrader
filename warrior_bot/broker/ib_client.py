from __future__ import annotations

import asyncio
import logging

from ib_async import IB, Contract, Stock

from warrior_bot.config import AppConfig
from warrior_bot.logging_setup import alert

logger = logging.getLogger("warrior_bot.broker")


class IBClient:
    """Thin connect/reconnect wrapper around ib_async.IB.

    Owns exactly one IB() instance for the process. Callers get the
    underlying `ib` object via `.ib` for everything else (placing orders,
    subscribing to data, etc.) — this class only owns lifecycle.
    """

    # IBKR sends these through errorEvent even though they're routine status
    # notices, not real errors (market data farm connection established/OK,
    # HMDS farm connection established/OK) -- downgraded to DEBUG so a
    # normal session doesn't spam WARNING on every symbol subscription.
    #
    # 162 is here because the one-shot reqScannerDataAsync pattern makes
    # IBKR acknowledge its own auto-cancel through this channel on EVERY
    # scan: at refresh_seconds=5 that was 8,412 WARNING lines on
    # 2026-09-28, i.e. 82% of the day's entire WARNING volume, which buried
    # the 519 genuine code=322 warnings that mattered. scan_candidates
    # inspects 162 itself (see market_data.ScannerRefused), so nothing is
    # lost by not shouting about it here.
    _INFORMATIONAL_ERROR_CODES = {162, 2104, 2106, 2107, 2108, 2119, 2158}

    # Errors that mean an order did not do what we asked. These never
    # reached Discord before -- combined with replacement stops having had
    # no status listener, a rejected protective stop was invisible on every
    # channel until the reconciliation watchdog happened to notice.
    #
    # 202 ("Order Cancelled") is deliberately NOT here. It was, briefly, and
    # that was a mistake: this bot cancels orders constantly by design --
    # every breakeven move, every stop resize, every flatten -- so it fired
    # 13 kill_switch alerts for entirely routine cancels in two days of
    # light trading, and would scale with activity. An unexpected
    # cancellation still surfaces via the reconciliation watchdog, which
    # checks actual protection rather than intent.
    _ORDER_REJECTION_CODES = {
        110,  # price does not conform to the minimum price variation
        201,  # order rejected (margin, compliance, etc.)
        203,  # security not available/allowed
        321,  # server error validating the request
        404,  # shares not available (locate)
        10326,  # OCA group revision not allowed
    }

    # How often to actively probe the connection, and how long to wait for
    # the probe before declaring the link dead.
    HEARTBEAT_INTERVAL_SECONDS = 30.0
    HEARTBEAT_TIMEOUT_SECONDS = 10.0

    def __init__(self, config: AppConfig):
        self.config = config
        self.ib = IB()
        self._contract_cache: dict[str, Contract] = {}
        self.ib.disconnectedEvent += self._on_disconnected
        self.ib.errorEvent += self._on_error
        self._reconnecting = False
        # Retained so asyncio can't garbage-collect the reconnect mid-sleep
        # (it holds only a weak reference to bare ensure_future tasks).
        self._reconnect_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None

    def _on_error(self, reqId: int, errorCode: int, errorString: str, contract) -> None:
        # ib_async's own error channel -- e.g. historical-data pacing
        # violations (366) or subscription failures never surface
        # anywhere else, since nothing else in this codebase listens to
        # errorEvent. Without this hook, that whole class of failure is
        # invisible in data/warrior_bot.log no matter the log level.
        level = logging.DEBUG if errorCode in self._INFORMATIONAL_ERROR_CODES else logging.WARNING
        logger.log(level, "IBKR errorEvent reqId=%s code=%s: %s (%s)", reqId, errorCode, errorString, contract)
        if errorCode in self._ORDER_REJECTION_CODES:
            # reqId is the orderId for order-scoped errors.
            alert(
                f"IBKR rejected/cancelled order {reqId} (code {errorCode}): {errorString} -- "
                "a protective order may be missing; the reconciliation watchdog will verify",
                channel="kill_switch",
            )

    async def connect(self) -> None:
        t = self.config.trading
        logger.info("Connecting to IBKR at %s:%s (clientId=%s, mode=%s)", t.host, t.port, t.client_id, t.mode)
        # timeout/raiseSyncErrors are explicit because ib_async defaults to
        # timeout=4 with raiseSyncErrors=False: its startup sync (positions,
        # open orders, completed orders, account updates, executions) each
        # get 4s, a timeout is logged and SWALLOWED, and connectedEvent
        # fires anyway. On a loaded Gateway after an outage that yields a
        # "successful" connect with no open orders synced -- so
        # resync_after_reconnect finds nothing to re-wire and every fill
        # listener stays dead for the rest of the session while the process
        # believes it is healthy. Raising instead turns that into a retry.
        await self.ib.connectAsync(
            t.host, t.port, clientId=t.client_id, timeout=20, raiseSyncErrors=True
        )
        self._verify_account()
        logger.info(
            "Connected. Server version=%s, account=%s",
            self.ib.client.serverVersion(),
            t.account or "(the only one)",
        )

    def _verify_account(self) -> None:
        """Refuse to trade an ambiguous or wrong account.

        Two states are worth failing the connect over rather than discovering
        later. More than one account managed with none configured: IBKR rejects
        every order in that state, and an unscoped position read would return
        another bot's holdings straight into the reconciliation watchdog, which
        flattens what it does not recognise. And a configured account the login
        does not manage -- a typo, or an id pasted from the wrong place -- which
        would otherwise mean silently trading the wrong account.

        No managed accounts at all only warns: IB sometimes reports nothing here
        before it settles, and refusing over that is worse than carrying on.
        """
        configured = (self.config.trading.account or "").strip()
        managed = [a for a in (self.ib.managedAccounts() or []) if a]

        if not managed:
            logger.warning("IBKR reported no managed accounts; cannot verify trading.account")
            return

        if configured and configured not in managed:
            raise RuntimeError(
                f"trading.account {configured!r} is not managed by this login "
                f"(it manages {', '.join(managed)}). Fix config/config.yaml."
            )

        if not configured and len(managed) > 1:
            raise RuntimeError(
                f"this login manages {len(managed)} accounts ({', '.join(managed)}) but "
                "trading.account is blank. IBKR rejects orders that do not name an account "
                "when more than one is managed, and an unscoped position read would return "
                "the other account's holdings. Set trading.account in config/config.yaml."
            )

    def start_heartbeat(self) -> None:
        """Begins actively probing the connection.

        Nothing previously verified the link was alive. `isConnected()` is
        just `client.isReady()`, so a half-open socket or a Gateway whose
        API thread has wedged keeps reporting True, disconnectedEvent never
        fires, and every loop spins against a dead API -- which is how
        2026-09-28 produced 5h20m of silence."""
        if self._heartbeat_task is None or self._heartbeat_task.done():
            self._heartbeat_task = asyncio.ensure_future(self._heartbeat_loop())

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self.HEARTBEAT_INTERVAL_SECONDS)
            if not self.ib.isConnected() or self._reconnecting:
                continue
            try:
                await asyncio.wait_for(
                    self.ib.reqCurrentTimeAsync(), timeout=self.HEARTBEAT_TIMEOUT_SECONDS
                )
            except Exception:
                logger.error(
                    "IBKR heartbeat timed out after %.0fs -- connection looks alive but is not "
                    "responding; forcing a disconnect to trigger reconnection",
                    self.HEARTBEAT_TIMEOUT_SECONDS,
                )
                alert(
                    "IBKR stopped responding to heartbeats -- forcing a reconnect", channel="kill_switch"
                )
                try:
                    self.ib.disconnect()
                except Exception:
                    logger.exception("Forced disconnect after heartbeat failure did not succeed")

    def _on_disconnected(self) -> None:
        # Set synchronously, BEFORE scheduling the coroutine: the flag used
        # to be set inside _reconnect_loop, so two disconnect events in the
        # same tick both passed this guard and started competing loops.
        if self._reconnecting:
            return
        self._reconnecting = True
        logger.warning("Disconnected from IBKR — scheduling reconnect")
        alert("Disconnected from IBKR — attempting to reconnect", channel="kill_switch")
        self._reconnect_task = asyncio.ensure_future(self._reconnect_loop())

    # Consecutive failed reconnect attempts before escalating from "just a
    # blip" (kill_switch, already alerted in _on_disconnected) to "trading
    # has likely stopped for the day" (limits) -- e.g. an expired Gateway
    # session/login that needs a human to intervene, not something that
    # will resolve itself by retrying.
    RECONNECT_ALERT_THRESHOLD = 5

    async def _reconnect_loop(self) -> None:
        self._reconnecting = True
        delay = 2
        consecutive_failures = 0
        session_alert_sent = False
        try:
            while not self.ib.isConnected():
                logger.warning("Reconnecting in %ss...", delay)
                await asyncio.sleep(delay)
                try:
                    # Drop any half-open session first. Without it, IBKR can
                    # still consider the old clientId in use and reject
                    # every attempt with error 326 -- a permanent failure
                    # loop rather than a recovery.
                    try:
                        self.ib.disconnect()
                    except Exception:
                        logger.debug("Pre-reconnect disconnect failed (already down?)", exc_info=True)
                    await self.connect()
                except Exception:
                    logger.exception("Reconnect attempt failed")
                    consecutive_failures += 1
                    delay = min(delay * 2, 60)
                    if consecutive_failures >= self.RECONNECT_ALERT_THRESHOLD and not session_alert_sent:
                        alert(
                            f"IBKR reconnect has failed {consecutive_failures} times in a row -- "
                            "the bot may be unable to continue trading today (check Gateway login/session)",
                            channel="limits",
                        )
                        session_alert_sent = True
            if session_alert_sent:
                alert("IBKR reconnected -- trading can resume", channel="limits")
            alert("Reconnected to IBKR", channel="kill_switch")
        finally:
            self._reconnecting = False

    def disconnect(self) -> None:
        if self.ib.isConnected():
            self.ib.disconnect()

    async def qualify_stock(self, symbol: str, exchange: str = "SMART", currency: str = "USD") -> Contract:
        cached = self._contract_cache.get(symbol)
        if cached is not None:
            return cached
        contract = Stock(symbol, exchange, currency)
        qualified = await self.ib.qualifyContractsAsync(contract)
        if not qualified:
            raise ValueError(f"Could not qualify contract for symbol {symbol!r}")
        self._contract_cache[symbol] = qualified[0]
        return qualified[0]
