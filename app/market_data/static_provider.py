from datetime import datetime, timezone
from decimal import Decimal
from random import uniform

from app.market_data.base import (
    MarketDataError,
    MarketDataProvider,
    MarketQuote,
    ProviderStatus,
)
from app.market_data.scanner import MarketScanner


class StaticMarketDataProvider(MarketDataProvider):
    """Fake offline market data, always labeled as static."""

    name = "Static simulation"
    live = False
    stale_after_seconds = float("inf")

    def __init__(self, scanner: MarketScanner | None = None):
        self.scanner = scanner or MarketScanner()
        self._prices = {
            "BTC": Decimal("115000.00"),
            "ETH": Decimal("4300.00"),
            "DOGE": Decimal("0.2250"),
            "SOL": Decimal("185.00"),
            "XRP": Decimal("2.95"),
            "LTC": Decimal("122.00"),
            "ADA": Decimal("0.88"),
            "LINK": Decimal("24.50"),
            "AVAX": Decimal("26.00"),
        }
        self._spread_pct = {
            "BTC": Decimal("0.025"), "ETH": Decimal("0.030"),
            "DOGE": Decimal("0.080"), "SOL": Decimal("0.050"),
            "XRP": Decimal("0.060"), "LTC": Decimal("0.070"),
            "ADA": Decimal("0.080"), "LINK": Decimal("0.070"),
            "AVAX": Decimal("0.080"),
        }
        self._last_update: datetime | None = None

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(self._prices)

    def _quote(self, symbol: str) -> MarketQuote:
        if symbol not in self._prices:
            raise MarketDataError(f"Unsupported static symbol: {symbol}")
        move = Decimal(str(uniform(-0.001, 0.001)))
        midpoint = self._prices[symbol] * (Decimal("1") + move)
        self._prices[symbol] = midpoint
        half_spread = midpoint * (self._spread_pct[symbol] / Decimal("100")) / 2
        now = datetime.now(timezone.utc)
        quote = MarketQuote(
            symbol=symbol,
            bid=midpoint - half_spread,
            ask=midpoint + half_spread,
            timestamp=now,
            received_at=now,
            provider=self.name,
        )
        self._last_update = now
        self.scanner.record(quote)
        return quote

    def get_quotes(self) -> list[MarketQuote]:
        return [self._quote(symbol) for symbol in self._prices]

    def get_quote(self, symbol: str) -> MarketQuote:
        return self._quote(symbol.strip().upper())

    def status(self) -> ProviderStatus:
        return ProviderStatus(
            name=self.name,
            live=False,
            connected=True,
            last_update=self._last_update,
        )
