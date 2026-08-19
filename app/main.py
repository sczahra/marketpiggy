from __future__ import annotations

import os
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote as url_quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.broker.paper import OrderRejected, PaperBroker
from app.market_data.base import MarketDataError, MarketQuote, ProviderStatus
from app.market_data.coinbase_provider import CoinbaseProvider, DEFAULT_SYMBOLS
from app.market_data.observations import ObservationSampler, ObservationStore
from app.market_data.scanner import MarketScanner, ScannerRow
from app.market_data.static_provider import StaticMarketDataProvider
from app.strategy import (
    BaselineMomentumStrategy,
    DecisionStore,
    StrategyConfig,
    StrategyRunner,
)


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR.parent / "data"
DATABASE_PATH = os.getenv("MARKETPIGGY_DB", str(DATA_DIR / "marketpiggy.db"))
PROVIDER_MODE = os.getenv("MARKETPIGGY_MARKET_PROVIDER", "coinbase").strip().lower()

scanner = MarketScanner(window_seconds=60)
if PROVIDER_MODE == "coinbase":
    configured_symbols = tuple(
        symbol.strip().upper()
        for symbol in os.getenv(
            "MARKETPIGGY_COINBASE_SYMBOLS", ",".join(DEFAULT_SYMBOLS)
        ).split(",")
        if symbol.strip()
    )
    market_data = CoinbaseProvider(
        symbols=configured_symbols,
        stale_after_seconds=float(os.getenv("MARKETPIGGY_STALE_SECONDS", "15")),
        scanner=scanner,
    )
elif PROVIDER_MODE == "static":
    static_move = os.getenv("MARKETPIGGY_STATIC_MOVE_PCT")
    market_data = StaticMarketDataProvider(
        scanner=scanner, deterministic_move_pct=static_move
    )
    configured_symbols = market_data.symbols
else:
    raise RuntimeError("MARKETPIGGY_MARKET_PROVIDER must be 'coinbase' or 'static'")

broker = PaperBroker(
    DATABASE_PATH,
    starting_balance=os.getenv("MARKETPIGGY_STARTING_BALANCE", "10.00"),
    friction_rate=os.getenv("MARKETPIGGY_FRICTION_RATE", "0.001"),
)
observation_store = ObservationStore(
    DATABASE_PATH,
    sample_interval_seconds=float(os.getenv("MARKETPIGGY_OBSERVATION_SECONDS", "1")),
)
observation_sampler = ObservationSampler(market_data, observation_store)
strategy_config = StrategyConfig(
    entry_momentum_pct=os.getenv("MARKETPIGGY_STRATEGY_ENTRY_MOMENTUM_PCT", "0.20"),
    max_spread_pct=os.getenv("MARKETPIGGY_STRATEGY_MAX_SPREAD_PCT", "0.20"),
    max_volatility_pct=os.getenv("MARKETPIGGY_STRATEGY_MAX_VOLATILITY_PCT", "0.35"),
    position_size_fraction=os.getenv("MARKETPIGGY_STRATEGY_POSITION_FRACTION", "0.25"),
    stop_loss_pct=os.getenv("MARKETPIGGY_STRATEGY_STOP_LOSS_PCT", "1.00"),
    take_profit_pct=os.getenv("MARKETPIGGY_STRATEGY_TAKE_PROFIT_PCT", "1.50"),
    max_holding_seconds=int(os.getenv("MARKETPIGGY_STRATEGY_MAX_HOLD_SECONDS", "900")),
    reversal_momentum_pct=os.getenv("MARKETPIGGY_STRATEGY_REVERSAL_PCT", "-0.10"),
    evaluation_interval_seconds=float(os.getenv("MARKETPIGGY_STRATEGY_EVALUATION_SECONDS", "3")),
    cooldown_seconds=int(os.getenv("MARKETPIGGY_STRATEGY_COOLDOWN_SECONDS", "60")),
    minimum_observations=int(os.getenv("MARKETPIGGY_STRATEGY_MIN_OBSERVATIONS", "5")),
)
decision_store = DecisionStore(DATABASE_PATH)
strategy_runner = StrategyRunner(
    BaselineMomentumStrategy(strategy_config), market_data, scanner, broker, decision_store
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await market_data.start()
    await observation_sampler.start()
    await strategy_runner.start()
    try:
        yield
    finally:
        await strategy_runner.stop()
        await observation_sampler.stop()
        await market_data.stop()


app = FastAPI(
    title="MarketPiggy",
    description="Local-only crypto paper trading simulator.",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


def _redirect(message: str, *, error: bool = False) -> RedirectResponse:
    kind = "error" if error else "message"
    return RedirectResponse(f"/?{kind}={url_quote(message)}", status_code=303)


def _current_rows() -> list[ScannerRow]:
    return scanner.rows(market_data.get_quotes())


def _serialize_quote(quote: MarketQuote) -> dict[str, object]:
    return {
        "symbol": quote.symbol,
        "bid": str(quote.bid),
        "ask": str(quote.ask),
        "midpoint": str(quote.midpoint),
        "spread": str(quote.spread),
        "spread_pct": str(quote.spread_pct),
        "timestamp": quote.timestamp.isoformat(),
        "provider": quote.provider,
        "age_seconds": round(quote.age_seconds, 2),
        "stale": quote.stale,
    }


def _serialize_status(status: ProviderStatus) -> dict[str, object]:
    return {
        "name": status.name,
        "live": status.live,
        "connected": status.connected,
        "last_update": status.last_update.isoformat() if status.last_update else None,
        "error": status.error,
        "reconnect_attempts": status.reconnect_attempts,
    }


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    rows = _current_rows()
    quotes_by_symbol = {row.quote.symbol: row.quote for row in rows}
    unmarked = broker.portfolio()
    mark_quote = quotes_by_symbol.get(unmarked.symbol) if unmarked.symbol else None
    portfolio = broker.portfolio(mark_quote.bid if mark_quote else None)
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "portfolio": portfolio,
            "active_session": broker.active_session(),
            "held_quote": mark_quote,
            "rows": rows,
            "trades": broker.trades(),
            "friction_rate": broker.friction_rate,
            "reset_count": broker.reset_count(),
            "provider_status": market_data.status(),
            "provider_mode": PROVIDER_MODE,
            "configured_symbols": configured_symbols,
            "stale_after_seconds": market_data.stale_after_seconds,
            "strategy_status": strategy_runner.status(),
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
        },
    )


@app.post("/simulation/reset")
async def reset_simulation(
    starting_balance: str = Form(...), confirm_reset: str | None = Form(None)
):
    if confirm_reset != "yes":
        return _redirect("Confirm the portfolio reset before continuing", error=True)
    try:
        strategy_runner.disable()
        broker.start_new_simulation(starting_balance, preserve_history=True)
    except OrderRejected as exc:
        return _redirect(str(exc), error=True)
    return _redirect(
        f"Started a new paper simulation with ${starting_balance}; trade history was preserved and autopilot is off"
    )


@app.post("/autopilot/enable")
async def enable_autopilot(confirm_paper: str | None = Form(None)):
    if confirm_paper != "yes":
        return _redirect("Confirm paper-only autonomous trading before enabling", error=True)
    strategy_runner.enable()
    return _redirect("Paper-only autopilot enabled")


@app.post("/autopilot/disable")
async def disable_autopilot():
    strategy_runner.disable()
    return _redirect("Autopilot disabled; any open position was left unchanged")


@app.post("/trade/buy")
async def buy(symbol: str = Form(...), dollars: str = Form(...)):
    try:
        quote = market_data.get_trade_quote(symbol)
        broker.buy(
            symbol,
            dollars,
            bid=quote.bid,
            ask=quote.ask,
            provider_name=quote.provider,
            quote_timestamp=quote.timestamp,
            received_at=quote.received_at,
        )
    except (OrderRejected, MarketDataError) as exc:
        return _redirect(str(exc), error=True)
    return _redirect(f"Bought {symbol.upper()} with ${dollars} in paper funds")


@app.post("/trade/sell")
async def sell(symbol: str = Form(...), quantity: str = Form(...)):
    try:
        quote = market_data.get_trade_quote(symbol)
        broker.sell(
            symbol,
            quantity,
            bid=quote.bid,
            ask=quote.ask,
            provider_name=quote.provider,
            quote_timestamp=quote.timestamp,
            received_at=quote.received_at,
        )
    except (OrderRejected, MarketDataError) as exc:
        return _redirect(str(exc), error=True)
    return _redirect(f"Sold {quantity} {symbol.upper()} in paper mode")


@app.get("/api/quotes")
async def quotes():
    return [_serialize_quote(quote) for quote in market_data.get_quotes()]


@app.get("/api/market")
async def market():
    return {
        "provider": _serialize_status(market_data.status()),
        "quotes": [
            {
                **_serialize_quote(row.quote),
                "short_return_pct": str(row.short_return_pct),
                "volatility_pct": str(row.volatility_pct),
            }
            for row in _current_rows()
        ],
    }


@app.get("/api/strategy")
async def strategy_status():
    return strategy_runner.status()


@app.get("/health")
async def health():
    status = market_data.status()
    return {
        "status": "ok",
        "mode": "paper",
        "market_data": PROVIDER_MODE,
        "market_connected": status.connected,
    }
