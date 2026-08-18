import os
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote as url_quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.broker.paper import OrderRejected, PaperBroker
from app.market_data.static_provider import StaticMarketDataProvider


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR.parent / "data"

app = FastAPI(title="MarketPiggy", description="Local-only crypto paper trading simulator.")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

market_data = StaticMarketDataProvider()
broker = PaperBroker(
    os.getenv("MARKETPIGGY_DB", str(DATA_DIR / "marketpiggy.db")),
    starting_balance=os.getenv("MARKETPIGGY_STARTING_BALANCE", "10.00"),
    friction_rate=os.getenv("MARKETPIGGY_FRICTION_RATE", "0.001"),
)


def _redirect(message: str, *, error: bool = False) -> RedirectResponse:
    kind = "error" if error else "message"
    return RedirectResponse(f"/?{kind}={url_quote(message)}", status_code=303)


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    quotes = market_data.get_quotes()
    quotes_by_symbol = {quote.symbol: quote for quote in quotes}
    unmarked = broker.portfolio()
    mark_bid = (
        Decimal(str(quotes_by_symbol[unmarked.symbol].bid)) if unmarked.symbol else None
    )
    portfolio = broker.portfolio(mark_bid)
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "portfolio": portfolio,
            "quotes": quotes,
            "trades": broker.trades(),
            "friction_rate": broker.friction_rate,
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
        },
    )


@app.post("/trade/buy")
async def buy(symbol: str = Form(...), dollars: str = Form(...)):
    try:
        market_quote = market_data.get_quote(symbol)
        broker.buy(
            symbol,
            dollars,
            bid=Decimal(str(market_quote.bid)),
            ask=Decimal(str(market_quote.ask)),
        )
    except (OrderRejected, KeyError) as exc:
        return _redirect(str(exc), error=True)
    return _redirect(f"Bought {symbol.upper()} with ${dollars} in paper funds")


@app.post("/trade/sell")
async def sell(symbol: str = Form(...), quantity: str = Form(...)):
    try:
        market_quote = market_data.get_quote(symbol)
        broker.sell(
            symbol,
            quantity,
            bid=Decimal(str(market_quote.bid)),
            ask=Decimal(str(market_quote.ask)),
        )
    except (OrderRejected, KeyError) as exc:
        return _redirect(str(exc), error=True)
    return _redirect(f"Sold {quantity} {symbol.upper()} in paper mode")


@app.get("/api/quotes")
async def quotes():
    return [
        {"symbol": item.symbol, "bid": item.bid, "ask": item.ask,
         "midpoint": item.midpoint, "spread": item.spread,
         "spread_pct": item.spread_pct}
        for item in market_data.get_quotes()
    ]


@app.get("/health")
async def health():
    return {"status": "ok", "mode": "paper", "market_data": "static"}
