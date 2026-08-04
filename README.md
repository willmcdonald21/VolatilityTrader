# warrior-bot

Automated IBKR day-trading bot modeled on Warrior Trading / Ross Cameron momentum setups: gap-and-go, bull flag, ABCD, and VWAP reversion / red-to-green. Paper trading only until you deliberately opt into live trading.

## Status

Environment, core infrastructure (broker connectivity, risk manager, order manager, bracket orders, SQLite trade journal), and all four strategies are built and unit-tested (`pytest tests/unit` — 50 passing). **Not yet connected to a live IB Gateway paper session** — that's the next step, and it requires your own IBKR login, so it isn't automated here.

## One-time setup

1. Install [IB Gateway](https://www.interactivebrokers.com/en/trading/ibgateway-stable.php) (lighter than full TWS). Log in choosing **Paper Trading** — your paper account is provisioned automatically alongside your normal login.
2. In IB Gateway: **Configure > Settings > API > Settings** — check "Enable ActiveX and Socket Clients", confirm the socket port (4002 for Gateway paper), add `127.0.0.1` to Trusted IPs, and **uncheck** "Read-Only API" (must be off to place orders).
3. From this project directory:
   ```
   .venv\Scripts\Activate.ps1
   ```

## Verify connectivity (no orders placed)

```
python scripts\dry_run.py
```
Runs the full scan -> signal -> risk pipeline and logs what it *would* trade, without ever calling `placeOrder`. Use this first, and every time you change a strategy parameter.

## Run for real (paper)

```
python -m warrior_bot.main
```

## Emergency stop

```
python scripts\kill_switch.py
```
Cancels every open order and flattens every position on the connected account. Also honors a flag file at `data/KILL_SWITCH` — create that file (any content) to halt new entries without restarting the process.

## Review performance

```
python scripts\daily_report.py
```
Win rate, PnL, and average R-multiple per strategy, read from the SQLite trade journal (`data/journal.sqlite3`). This journal — not a backtest — is the primary feedback loop for tuning `config/config.yaml`.

## Tests

```
pytest tests\unit          # pattern detectors, risk math, bracket construction — no IBKR needed
pytest tests\integration   # requires a running IB Gateway paper session; skips itself otherwise
```

## Going live (later, deliberately)

Nothing here does this automatically. When you're ready, after enough paper-trading evidence: set `trading.mode: live` and `trading.i_understand_live_trading: true` in `config/config.yaml`, point `trading.port` at 4001 (Gateway) or 7496 (TWS), and re-review every number in the `risk:` section for real capital before running.
