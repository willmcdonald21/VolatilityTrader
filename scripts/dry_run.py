"""Run the full scan -> signal -> risk pipeline against paper, logging what
the bot *would* do without ever calling placeOrder. Use this to sanity-check
a strategy before letting it trade for real (even in paper)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from warrior_bot.config import load_config
from warrior_bot.main import WarriorBot


async def main() -> None:
    config = load_config()
    bot = WarriorBot(config, dry_run=True)
    await bot.start()
    print("Dry run active — watching for signals. Ctrl+C to stop.")
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await bot.stop()


if __name__ == "__main__":
    asyncio.run(main())
