# MarketPiggy

MarketPiggy is a local-only cryptocurrency paper-trading simulator. Milestone 3 adds one deliberately simple, deterministic baseline momentum strategy on top of the public market-data scanner and SQLite paper broker.

## Run locally on Windows

From PowerShell in the repository directory:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000). The provider panel and scanner update automatically. Stop Uvicorn cleanly with `Ctrl+C`.

## Market-data providers

Coinbase live mode is the default:

```powershell
$env:MARKETPIGGY_MARKET_PROVIDER = "coinbase"
uvicorn app.main:app --reload
```

It connects to Coinbase Advanced Trade's public market-data WebSocket, subscribes separately to `ticker` and `heartbeats`, and uses the ticker's `best_bid` and `best_ask`. No Coinbase account, API key, secret, or trading credential is used. The default live subset is `BTC`, `ETH`, `SOL`, `DOGE`, and `XRP`, mapped internally to their `-USD` products.

For offline development and testing, select the explicitly labeled fake provider:

```powershell
$env:MARKETPIGGY_MARKET_PROVIDER = "static"
uvicorn app.main:app --reload
```

Static prices are never labeled as live and are never silently substituted when Coinbase is unavailable.

Optional configuration:

```powershell
$env:MARKETPIGGY_COINBASE_SYMBOLS = "BTC,ETH,SOL,DOGE,XRP"
$env:MARKETPIGGY_STALE_SECONDS = "15"
$env:MARKETPIGGY_OBSERVATION_SECONDS = "1"
$env:MARKETPIGGY_STARTING_BALANCE = "10.00"
$env:MARKETPIGGY_FRICTION_RATE = "0.001"
$env:MARKETPIGGY_DB = "data/marketpiggy.db"
```

`MARKETPIGGY_STARTING_BALANCE` is only the first-database default. The dashboard can start or reset a simulation with another positive balance while retaining trade history.

## Paper-only autopilot

The dashboard has one compact **Autopilot** control for `Baseline Momentum v1.0`. It is always **OFF after the app starts**, regardless of its prior state. Turning it on requires checking the paper-only confirmation. Turning it off immediately stops future autonomous actions and does not liquidate an open position. Starting or resetting a simulation also turns autopilot off; existing trade and strategy-decision history remains in SQLite.

The strategy can hold only one asset and never pyramids. With no position it chooses the symbol with the strongest qualifying 60-second momentum (alphabetical symbol breaks an exact tie), then invests 25% of available paper cash. With a position it evaluates exits in this order: stop loss, take profit, maximum holding time, then momentum reversal. A completed sell starts a re-entry cooldown.

Exact defaults:

| Parameter | Default | Environment variable |
|---|---:|---|
| Entry momentum | `+0.20%` | `MARKETPIGGY_STRATEGY_ENTRY_MOMENTUM_PCT` |
| Maximum spread | `0.20%` | `MARKETPIGGY_STRATEGY_MAX_SPREAD_PCT` |
| Maximum interval volatility | `0.35%` | `MARKETPIGGY_STRATEGY_MAX_VOLATILITY_PCT` |
| Position size | `25%` of available cash | `MARKETPIGGY_STRATEGY_POSITION_FRACTION` |
| Stop loss | `1.00%` below average entry (current bid) | `MARKETPIGGY_STRATEGY_STOP_LOSS_PCT` |
| Take profit | `1.50%` above average entry (current bid) | `MARKETPIGGY_STRATEGY_TAKE_PROFIT_PCT` |
| Maximum hold | `900` seconds | `MARKETPIGGY_STRATEGY_MAX_HOLD_SECONDS` |
| Momentum reversal | `-0.10%` | `MARKETPIGGY_STRATEGY_REVERSAL_PCT` |
| Evaluation interval | `3` seconds | `MARKETPIGGY_STRATEGY_EVALUATION_SECONDS` |
| Post-exit cooldown | `60` seconds | `MARKETPIGGY_STRATEGY_COOLDOWN_SECONDS` |
| Minimum observations | `5` in the rolling window | `MARKETPIGGY_STRATEGY_MIN_OBSERVATIONS` |

Invalid configuration fails clearly during startup. Strategy decisions are persisted with the simulation session, strategy/version, action, reason code and explanation, signal values, execution result, and resulting trade ID. Repeated non-executed `HOLD`/`SKIP` outcomes with the same reason and symbol are logged at most once per 30 seconds, and the log is bounded to the newest 1,000 records.

### Scanner eligibility lights

The small light beside every scanner symbol comes from `BaselineMomentumStrategy`'s own entry-rule evaluation—the dashboard does not maintain a second eligibility engine:

- **Green:** the quote is fresh and usable, and history, momentum, spread, and volatility all pass.
- **Yellow:** the quote is fresh and usable, but exactly one of those four normal entry rules fails.
- **Red:** two or more normal entry rules fail, or the quote is stale/disconnected/unusable.

Hover or tap the light for a compact explanation such as `Eligible`, `Waiting for history`, `Momentum below threshold`, or `2 rules failing`. Lights describe entry-signal eligibility only; Autopilot can still be off, in cooldown, or already holding a position.

### Temporary synthetic TEST coin

`TEST` is an obviously synthetic, paper-only verification symbol. It is **OFF by default**, is never requested from Coinbase, and never replaces a missing or stale Coinbase quote. When enabled, it is labeled `TEST DATA ENABLED` and `SYNTHETIC`, uses provider provenance `Synthetic TEST data`, and follows the same scanner, strategy rules, freshness enforcement, `StrategyRunner`, and `PaperBroker` path as every other paper trade.

The TEST feed rises deterministically by `0.40%` per generated observation with a `0.05%` spread. It therefore progresses naturally from red (history and momentum fail), through yellow (only history fails), to green after the normal five-observation minimum. Continued movement exercises the normal `TAKE_PROFIT` exit and cooldown; the strategy contains no TEST-specific bypass.

Shortest verification workflow in PowerShell:

```powershell
# First stop Uvicorn with Ctrl+C, then enable TEST for one process run.
$env:MARKETPIGGY_TEST_COIN = "1"
uvicorn app.main:app

# After observing the paper buy, exit, and cooldown, stop with Ctrl+C.
Remove-Item Env:MARKETPIGGY_TEST_COIN
uvicorn app.main:app
```

In the enabled run, open the dashboard and explicitly turn Autopilot on. Watch TEST move red → yellow → green, receive a normal autonomous paper buy, then exit through `TAKE_PROFIT` and enter `Cooldown`. Restarting with `MARKETPIGGY_TEST_COIN` unset (or set to `0`) removes TEST and restores normal operation. Only `0` and `1` are accepted.

### Live dashboard refresh

The open dashboard makes one read-only request to `/api/dashboard` every two seconds. That single non-overlapping polling loop updates portfolio value, cash, holding details, realized and unrealized P/L, total return, the open-position mark and close controls, scanner values and unchanged eligibility lights, Autopilot status and evaluation recency, and the newest-first Trade Log. A transient read error leaves the last good values visible and retries on the next interval.

When a position is open, its scanner row receives a restrained highlight and `HOLDING` badge. A newly observed paper fill is inserted without a page reload, briefly highlights its Trade Log row, and shows a compact `BOUGHT SYMBOL` or `SOLD SYMBOL` cue. JavaScript only renders server-provided state; it contains no strategy evaluation, order selection, pricing, or execution logic.

Short browser smoke test:

1. Start MarketPiggy and leave the dashboard open without refreshing it.
2. Explicitly enable Autopilot.
3. Confirm scanner values and `Evaluated … ago` continue changing.
4. When a paper trade occurs, confirm the cards and open-position panel update, the held scanner row shows `HOLDING`, and the new Trade Log row briefly highlights.
5. Confirm a later exit removes the open-position panel, updates P/L, and adds the newest SELL row.
6. Stop cleanly with `Ctrl+C`.

The temporary TEST mode above provides a deterministic buy, take-profit exit, and cooldown for this smoke test.

### Deterministic offline smoke test

This mode uses the normal GUI and the explicitly fake static provider. The fixed `+0.10%` change per quote makes an entry predictable without weakening the positive-momentum rule:

```powershell
$env:MARKETPIGGY_MARKET_PROVIDER = "static"
$env:MARKETPIGGY_STATIC_MOVE_PCT = "0.10"
uvicorn app.main:app
```

Open the dashboard, start/reset the paper simulation if desired, check **Paper trades only**, and turn autopilot on. After five observations, the strategy deterministically buys 25% of paper cash in the strongest qualifying symbol (alphabetical symbol order breaks an exact computed tie). Watch the status and decision reason change from `Watching` to `Holding`; the fixed upward feed will eventually exercise the take-profit exit and `Cooldown`. Turn autopilot off to confirm that it takes no further actions and leaves any position unchanged, then stop with `Ctrl+C`. This setting is only a deterministic fake-feed aid; omit `MARKETPIGGY_STATIC_MOVE_PCT` for the normal randomized static simulation.

## Safety and reconnect behavior

The default stale threshold is 15 seconds. A quote becomes unsafe when it exceeds that age or immediately when the live WebSocket disconnects. Unsafe quotes remain visibly marked stale, but paper buy and sell requests are rejected with a clear error. Fake prices are not used as a fallback.

After a disconnect, the Coinbase provider reconnects in the background with bounded exponential delays of 1, 2, 4, 8, 16, then 30 seconds. A successful connection resets the retry count. FastAPI shutdown cancels the reconnect wait, WebSocket task, and observation sampler so they do not delay exit.

## Scanner and observation sampling

The scanner keeps a rolling 60-second in-memory quote window:

- short-term return is the percent change from the oldest to newest midpoint in the window
- volatility is the population standard deviation of consecutive midpoint returns in that window

Current bid, ask, spread, return, volatility, quote age, and freshness appear in the dashboard. They remain informational unless the user explicitly turns on the single paper-only autopilot.

Current observations are sampled to the local SQLite database at most once per second per sampling pass. Each pass stores one snapshot for every currently available symbol, rather than every WebSocket tick. The stored fields are sample time, provider timestamp, provider, symbol, bid, ask, midpoint, and spread. This is a replay foundation, not a full tick archive.

## Simulation and trade provenance

Each new or reset paper simulation has a persistent session identity. New trades store that session together with the market-data provider, provider quote timestamp, and local receive timestamp. This prevents future comparisons from accidentally mixing separate simulations or static and live fills.

Trades created before this provenance schema remain unchanged and appear as **Legacy** because their exact provider and session cannot be established safely. MarketPiggy does not guess that old trades came from Coinbase or rewrite their historical execution data. This metadata is groundwork for trustworthy future comparisons; Milestone 2.1 does not add strategy analytics or replay UI.

## Tests

The automated suite uses fixture messages and temporary databases; it does not contact Coinbase:

```powershell
pytest
```

To observe the public live feed, run the app in default Coinbase mode and watch the provider panel change to `LIVE / Connected`, quote ages remain fresh, and scanner values update. Autopilot remains off until explicitly enabled. The `/api/market` and `/api/strategy` endpoints expose the current feed and runner status. If the socket disconnects or quotes become stale, autonomous entries and exits are blocked—there is no fake-data fallback.

## Milestone status and boundaries

Milestone 3 is paper trading only. It contains no exchange or brokerage order execution, Coinbase account integration, Robinhood credentials, multiple strategies, ML/LLM decisions, backtesting/replay, or real-money path. Both manual and autonomous paper orders execute through the same `PaperBroker` from current ask/bid plus adverse simulated friction, and only when market data is fresh.
