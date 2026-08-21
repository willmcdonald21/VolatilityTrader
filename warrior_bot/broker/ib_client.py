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
    _INFORMATIONAL_ERROR_CODES = {2104, 2106, 2107, 2108, 2119, 2158}

    def __init__(self, config: AppConfig):
        self.config = config
        self.ib = IB()
        self._contract_cache: dict[str, Contract] = {}
        self.ib.disconnectedEvent += self._on_disconnected
        self.ib.errorEvent += self._on_error
        self._reconnecting = False

    def _on_error(self, reqId: int, errorCode: int, errorString: str, contract) -> None:
        # ib_async's own error channel -- e.g. historical-data pacing
        # violations (162, 366) or subscription failures never surface
        # anywhere else, since nothing else in this codebase listens to
        # errorEvent. Without this hook, that whole class of failure is
        # invisible in data/warrior_bot.log no matter the log level.
        level = logging.DEBUG if errorCode in self._INFORMATIONAL_ERROR_CODES else logging.WARNING
        logger.log(level, "IBKR errorEvent reqId=%s code=%s: %s (%s)", reqId, errorCode, errorString, contract)

    async def connect(self) -> None:
        t = self.config.trading
        logger.info("Connecting to IBKR at %s:%s (clientId=%s, mode=%s)", t.host, t.port, t.client_id, t.mode)
        await self.ib.connectAsync(t.host, t.port, clientId=t.client_id)
        logger.info("Connected. Server version=%s", self.ib.client.serverVersion())

    def _on_disconnected(self) -> None:
        if self._reconnecting:
            return
        logger.warning("Disconnected from IBKR — scheduling reconnect")
        alert("Disconnected from IBKR — attempting to reconnect", channel="kill_switch")
        asyncio.ensure_future(self._reconnect_loop())

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
