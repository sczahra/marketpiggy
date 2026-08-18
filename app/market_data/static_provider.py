from dataclasses import dataclass
from random import uniform


@dataclass
class MarketQuote:
    symbol: str
    bid: float
    ask: float

    @property
    def midpoint(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    @property
    def spread_pct(self) -> float:
        if self.midpoint == 0:
            return 0.0
        return (self.spread / self.midpoint) * 100


class StaticMarketDataProvider:
    """
    Fake market-data source for Milestone 1.

    Prices drift slightly whenever quotes are requested.
    No network connection is used.
    """

    def __init__(self):
        self._prices = {
            "BTC": 115000.00,
            "ETH": 4300.00,
            "DOGE": 0.2250,
            "SOL": 185.00,
            "XRP": 2.95,
            "LTC": 122.00,
            "ADA": 0.88,
            "LINK": 24.50,
            "AVAX": 26.00,
        }

        self._spread_pct = {
            "BTC": 0.025,
            "ETH": 0.030,
            "DOGE": 0.080,
            "SOL": 0.050,
            "XRP": 0.060,
            "LTC": 0.070,
            "ADA": 0.080,
            "LINK": 0.070,
            "AVAX": 0.080,
        }

    def _move_price(self, symbol: str) -> float:
        current = self._prices[symbol]

        # Small random move of roughly +/- 0.10%.
        change_pct = uniform(-0.001, 0.001)

        updated = current * (1 + change_pct)
        self._prices[symbol] = updated

        return updated

    def get_quotes(self) -> list[MarketQuote]:
        quotes = []

        for symbol in self._prices:
            midpoint = self._move_price(symbol)

            spread_fraction = self._spread_pct[symbol] / 100
            half_spread = midpoint * spread_fraction / 2

            quotes.append(
                MarketQuote(
                    symbol=symbol,
                    bid=midpoint - half_spread,
                    ask=midpoint + half_spread,
                )
            )

        return quotes

    def get_quote(self, symbol: str) -> MarketQuote:
        symbol = symbol.upper()
        if symbol not in self._prices:
            raise KeyError(f"Unsupported symbol: {symbol}")
        midpoint = self._move_price(symbol)
        spread_fraction = self._spread_pct[symbol] / 100
        half_spread = midpoint * spread_fraction / 2
        return MarketQuote(
            symbol=symbol,
            bid=midpoint - half_spread,
            ask=midpoint + half_spread,
        )
