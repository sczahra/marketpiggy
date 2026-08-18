# MarketPiggy

MarketPiggy is a local-only cryptocurrency paper-trading simulator. Milestone 1 provides fake bid/ask quotes, manual paper buys and sells, conservative portfolio marking, realized and unrealized P/L, and SQLite-backed account and trade persistence through a simple FastAPI/Jinja2 dashboard.

## Run locally on Windows

From PowerShell in the repository directory:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000). Stop Uvicorn with `Ctrl+C`.

The account starts with $10.00. Optional environment variables may be set before launch:

```powershell
$env:MARKETPIGGY_STARTING_BALANCE = "10.00"
$env:MARKETPIGGY_FRICTION_RATE = "0.001"
$env:MARKETPIGGY_DB = "data/marketpiggy.db"
```

`MARKETPIGGY_FRICTION_RATE` is a decimal rate: `0.001` means 0.1%. The local database is created under `data/` by default and is ignored by Git.

## Tests

```powershell
pytest
```

## Milestone status and safety

Milestone 1 is complete: MarketPiggy supports one open crypto asset at a time, Decimal-based paper accounting, adverse simulated friction, persistent account state and trade history, and manual dashboard controls.

This milestone is **paper trading only**. It has no brokerage credentials, exchange connections, API secrets, live market data, or real-money execution path.

The next milestone boundary is strategy simulation and additional provider/broker implementations. Those features are intentionally not part of Milestone 1.
