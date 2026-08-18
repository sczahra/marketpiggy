from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.market_data.static_provider import StaticMarketDataProvider


BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(
    title="Crypto Paper Trader",
    description="Local-only cryptocurrency paper trading simulator.",
)

app.mount(
    "/static",
    StaticFiles(directory=BASE_DIR / "static"),
    name="static",
)

templates = Jinja2Templates(directory=BASE_DIR / "templates")

market_data = StaticMarketDataProvider()


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    quotes = market_data.get_quotes()

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "starting_balance": 10.00,
            "cash": 10.00,
            "portfolio_value": 10.00,
            "realized_pl": 0.00,
            "unrealized_pl": 0.00,
            "quotes": quotes,
        },
    )


@app.get("/api/quotes")
async def quotes():
    market_quotes = market_data.get_quotes()

    return [
        {
            "symbol": quote.symbol,
            "bid": quote.bid,
            "ask": quote.ask,
            "midpoint": quote.midpoint,
            "spread": quote.spread,
            "spread_pct": quote.spread_pct,
        }
        for quote in market_quotes
    ]


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "mode": "paper",
        "market_data": "static",
    }
