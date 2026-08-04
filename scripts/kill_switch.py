"""Emergency stop: cancel every open order and flatten every position on
the connected account. Connects with its own clientId so it works whether
or not the main bot process is still running.

Usage:
    python scripts/kill_switch.py [--no-flatten]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ib_async import IB

from warrior_bot.config import load_config
from warrior_bot.utils.panic import panic_stop


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-flatten", action="store_true", help="Cancel orders only, do not flatten positions")
    args = parser.parse_args()

    config = load_config()
    ib = IB()
    await ib.connectAsync(config.trading.host, config.trading.port, clientId=config.trading.client_id + 100)
    print(f"Connected to {config.trading.host}:{config.trading.port} (mode={config.trading.mode})")
    try:
        panic_stop(ib, flatten=not args.no_flatten)
        await asyncio.sleep(2)  # let cancel/flatten requests flush before disconnecting
    finally:
        ib.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
