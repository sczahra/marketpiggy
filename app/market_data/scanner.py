from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from statistics import pstdev
from threading import RLock

from app.market_data.base import MarketQuote


@dataclass(frozen=True)
class ScannerRow:
    quote: MarketQuote
    short_return_pct: Decimal
    volatility_pct: Decimal


class MarketScanner:
    """Explainable rolling return and interval-volatility calculations."""

    def __init__(self, window_seconds: float = 60.0) -> None:
        self.window = timedelta(seconds=window_seconds)
        self._observations: dict[str, deque[MarketQuote]] = defaultdict(deque)
        self._lock = RLock()

    def record(self, quote: MarketQuote) -> None:
        with self._lock:
            history = self._observations[quote.symbol]
            history.append(quote)
            cutoff = quote.received_at - self.window
            while history and history[0].received_at < cutoff:
                history.popleft()

    def metrics(self, symbol: str) -> tuple[Decimal, Decimal]:
        with self._lock:
            prices = [item.midpoint for item in self._observations.get(symbol, ())]
        if len(prices) < 2 or prices[0] == 0:
            return Decimal("0"), Decimal("0")
        short_return = ((prices[-1] / prices[0]) - Decimal("1")) * Decimal("100")
        interval_returns = [
            float(((current / previous) - Decimal("1")) * Decimal("100"))
            for previous, current in zip(prices, prices[1:])
            if previous != 0
        ]
        volatility = pstdev(interval_returns) if len(interval_returns) >= 2 else 0.0
        return short_return, Decimal(str(volatility))

    def observation_count(self, symbol: str) -> int:
        """Return the number of samples currently inside the rolling window."""
        with self._lock:
            return len(self._observations.get(symbol, ()))

    def rows(self, quotes: list[MarketQuote]) -> list[ScannerRow]:
        return [ScannerRow(quote, *self.metrics(quote.symbol)) for quote in quotes]
