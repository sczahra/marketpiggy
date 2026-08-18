from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal


class MarketDataError(ValueError):
    """Raised when a safe, current market quote isn't available."""


@dataclass(frozen=True)
class MarketQuote:
    symbol: str
    bid: Decimal
    ask: Decimal
    timestamp: datetime
    received_at: datetime
    provider: str
    stale: bool = False
    age_seconds: float = 0.0

    @property
    def midpoint(self) -> Decimal:
        return (self.bid + self.ask) / Decimal("2")

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid

    @property
    def spread_pct(self) -> Decimal:
        return (
            (self.spread / self.midpoint) * Decimal("100")
            if self.midpoint
            else Decimal("0")
        )

    def with_freshness(
        self, *, connected: bool, stale_after_seconds: float, now: datetime | None = None
    ) -> "MarketQuote":
        now = now or datetime.now(timezone.utc)
        age = max(0.0, (now - self.received_at).total_seconds())
        return replace(
            self,
            age_seconds=age,
            stale=(not connected or age > stale_after_seconds),
        )


@dataclass(frozen=True)
class ProviderStatus:
    name: str
    live: bool
    connected: bool
    last_update: datetime | None = None
    error: str | None = None
    reconnect_attempts: int = 0


class MarketDataProvider(ABC):
    stale_after_seconds: float

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    @abstractmethod
    def get_quotes(self) -> list[MarketQuote]:
        raise NotImplementedError

    @abstractmethod
    def get_quote(self, symbol: str) -> MarketQuote:
        raise NotImplementedError

    @abstractmethod
    def status(self) -> ProviderStatus:
        raise NotImplementedError

    def get_trade_quote(self, symbol: str) -> MarketQuote:
        quote = self.get_quote(symbol)
        if quote.stale:
            raise MarketDataError(
                f"{symbol.upper()} quote is stale or disconnected; paper order rejected"
            )
        return quote
