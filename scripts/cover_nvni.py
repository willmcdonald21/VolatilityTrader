"""One-off backstop: keep trying to buy-to-cover the leftover NVNI short at
premarket open and through the regular session, until it's flat or a
generous time budget runs out. Standalone from warrior_bot's own strategy
loop -- this is cleanup for a manually-created short, not a strategy signal.

Usage:
    python scripts/cover_nvni.py
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ib_async import IB, LimitOrder, Stock

from warrior_bot.config import load_config

ET = ZoneInfo("America/New_York")
PREMARKET_START = time(4, 0)
STOP_TRYING_AFTER = time(11, 0)  # give up for the day well into the regular session
SYMBOL = "NVNI"
MAX_ROUNDS = 200
ROUND_TIMEOUT_S = 30


def log(msg: str) -> None:
    print(f"{datetime.now(ET).isoformat()}  {msg}", flush=True)


async def wait_until_premarket() -> None:
    now = datetime.now(ET)
    today_start = now.replace(hour=PREMARKET_START.hour, minute=0, second=0, microsecond=0)
    today_stop = now.replace(hour=STOP_TRYING_AFTER.hour, minute=0, second=0, microsecond=0)
    if today_start <= now < today_stop:
        log("Already within today's premarket-to-cutoff window, starting immediately.")
        return
    target = today_start if now < today_start else today_start + timedelta(days=1)
    wait_s = (target - now).total_seconds()
    log(f"Waiting {wait_s:.0f}s until {target.isoformat()} (premarket open)...")
    await asyncio.sleep(wait_s)


async def cover_symbol(ib: IB, contract) -> bool:
    """One cancel+resubmit round. Returns True once fully flat."""
    ib.reqGlobalCancel()
    await asyncio.sleep(2)
    positions = await asyncio.wait_for(ib.reqPositionsAsync(), timeout=20)
    matches = [p for p in positions if p.position != 0 and p.contract.symbol == SYMBOL]
    if not matches:
        return True
    qty = abs(matches[0].position)
    action = "BUY" if matches[0].position < 0 else "SELL"
    ticker = ib.reqMktData(contract, "", False, False)
    await asyncio.sleep(2)
    price = ticker.ask if action == "BUY" else ticker.bid
    ib.cancelMktData(contract)
    if not price:
        log(f"No quote available for {SYMBOL}, skipping this round.")
        return False
    limit_price = round(price * (1.03 if action == "BUY" else 0.97), 2)
    log(f"qty={qty} action={action} ref_price={price} limit={limit_price}")
    order = LimitOrder(action, qty, limit_price, outsideRth=True, tif="DAY")
    trade = ib.placeOrder(contract, order)
    for _ in range(ROUND_TIMEOUT_S // 5):
        await asyncio.sleep(5)
        if trade.orderStatus.remaining == 0:
            break
    log(f"round result: filled={trade.orderStatus.filled} remaining={trade.orderStatus.remaining}")
    return trade.orderStatus.remaining == 0


async def main() -> None:
    await wait_until_premarket()

    config = load_config()
    ib = IB()
    await ib.connectAsync(config.trading.host, config.trading.port, clientId=config.trading.client_id + 150)
    log(f"Connected to {config.trading.host}:{config.trading.port} (mode={config.trading.mode})")

    contract = Stock(SYMBOL, "SMART", "USD")
    await ib.qualifyContractsAsync(contract)

    for round_num in range(MAX_ROUNDS):
        now_et = datetime.now(ET).time()
        if now_et >= STOP_TRYING_AFTER:
            log(f"Past {STOP_TRYING_AFTER} ET cutoff, stopping.")
            break
        try:
            flat = await cover_symbol(ib, contract)
        except Exception:
            log("round errored, will retry")
            flat = False
        if flat:
            log(f"{SYMBOL} is flat. Done.")
            break
        await asyncio.sleep(15)
    else:
        log(f"Hit MAX_ROUNDS={MAX_ROUNDS} without going flat.")

    positions = await asyncio.wait_for(ib.reqPositionsAsync(), timeout=20)
    positions = [p for p in positions if p.position != 0]
    log(f"FINAL positions: {[(p.contract.symbol, p.position) for p in positions]}")
    ib.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
