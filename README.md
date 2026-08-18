# MarketPiggy

MarketPiggy is a local-only cryptocurrency paper-trading simulator. Milestone 2 adds public near-live Coinbase Advanced Trade bid/ask data, a lightweight rolling market scanner, stale-quote safety, and sampled observations while preserving the Milestone 1 paper broker and SQLite accounting.

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

## Safety and reconnect behavior

The default stale threshold is 15 seconds. A quote becomes unsafe when it exceeds that age or immediately when the live WebSocket disconnects. Unsafe quotes remain visibly marked stale, but paper buy and sell requests are rejected with a clear error. Fake prices are not used as a fallback.

After a disconnect, the Coinbase provider reconnects in the background with bounded exponential delays of 1, 2, 4, 8, 16, then 30 seconds. A successful connection resets the retry count. FastAPI shutdown cancels the reconnect wait, WebSocket task, and observation sampler so they do not delay exit.

## Scanner and observation sampling

The scanner keeps a rolling 60-second in-memory quote window:

- short-term return is the percent change from the oldest to newest midpoint in the window
- volatility is the population standard deviation of consecutive midpoint returns in that window

Current bid, ask, spread, return, volatility, quote age, and freshness appear in the dashboard. These metrics are informational only and do not trigger trades.

Current observations are sampled to the local SQLite database at most once per second per sampling pass. Each pass stores one snapshot for every currently available symbol, rather than every WebSocket tick. The stored fields are sample time, provider timestamp, provider, symbol, bid, ask, midpoint, and spread. This is a replay foundation, not a full tick archive.

## Simulation and trade provenance

Each new or reset paper simulation has a persistent session identity. New trades store that session together with the market-data provider, provider quote timestamp, and local receive timestamp. This prevents future comparisons from accidentally mixing separate simulations or static and live fills.

Trades created before this provenance schema remain unchanged and appear as **Legacy** because their exact provider and session cannot be established safely. MarketPiggy does not guess that old trades came from Coinbase or rewrite their historical execution data. This metadata is groundwork for trustworthy future comparisons; Milestone 2.1 does not add strategy analytics or replay UI.

## Tests

The automated suite uses fixture messages and temporary databases; it does not contact Coinbase:

```powershell
pytest
```

To smoke-test the public live feed, run the app in default Coinbase mode and watch the provider panel change to `LIVE / Connected`, quote ages remain fresh, and scanner values update. The `/api/market` endpoint exposes the same current status and values for inspection.

## Milestone status and boundaries

Milestone 2 is paper trading only. It contains no exchange or brokerage order execution, Coinbase account integration, Robinhood credentials, automated strategies, strategy scoring, ML/LLM decisions, or real-money path. Manual paper orders continue to execute from current ask/bid plus adverse simulated friction only when market data is fresh.
