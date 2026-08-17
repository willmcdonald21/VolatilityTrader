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

    def __init__(self, config: AppConfig):
        self.config = config
        self.ib = IB()
        self._contract_cache: dict[str, Contract] = {}
        self.ib.disconnectedEvent += self._on_disconnected
        self._reconnecting = False

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

    async def _reconnect_loop(self) -> None:
        self._reconnecting = True
        delay = 2
        try:
            while not self.ib.isConnected():
                logger.warning("Reconnecting in %ss...", delay)
                await asyncio.sleep(delay)
                try:
                    await self.connect()
                except Exception:
                    logger.exception("Reconnect attempt failed")
                    delay = min(delay * 2, 60)
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
