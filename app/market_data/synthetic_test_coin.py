from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from app.market_data.base import MarketDataError, MarketDataProvider, MarketQuote, ProviderStatus
from app.market_data.scanner import MarketScanner


TEST_SYMBOL = "TEST"
TEST_PROVIDER_NAME = "Synthetic TEST data"


def test_coin_enabled(value: str | None) -> bool:
    normalized = (value or "0").strip()
    if normalized not in ("0", "1"):
        raise ValueError("MARKETPIGGY_TEST_COIN must be '0' or '1'")
    return normalized == "1"


class SyntheticTestCoinProvider(MarketDataProvider):
    """Deterministic, process-local TEST quotes for paper verification only."""

    name = TEST_PROVIDER_NAME
    live = False
    stale_after_seconds = float("inf")
    symbols = (TEST_SYMBOL,)

    def __init__(self, scanner: MarketScanner, *, move_pct: Decimal = Decimal("0.40")):
        self.scanner = scanner
        self.move_pct = move_pct
        self._price = Decimal("100")
        self._last_update: datetime | None = None

    def _next_quote(self) -> MarketQuote:
        self._price *= Decimal("1") + (self.move_pct / Decimal("100"))
        half_spread = self._price * Decimal("0.00025")
        now = datetime.now(timezone.utc)
        quote = MarketQuote(
            symbol=TEST_SYMBOL,
            bid=self._price - half_spread,
            ask=self._price + half_spread,
            timestamp=now,
            received_at=now,
            provider=self.name,
        )
        self._last_update = now
        self.scanner.record(quote)
        return quote

    def get_quotes(self) -> list[MarketQuote]:
        return [self._next_quote()]

    def get_quote(self, symbol: str) -> MarketQuote:
        if symbol.strip().upper() != TEST_SYMBOL:
            raise MarketDataError(f"Unsupported synthetic symbol: {symbol}")
        return self._next_quote()

    def status(self) -> ProviderStatus:
        return ProviderStatus(self.name, False, True, self._last_update)


class CompositeMarketDataProvider(MarketDataProvider):
    """Keep a real/static primary provider isolated from supplemental TEST data."""

    def __init__(self, primary: MarketDataProvider, supplemental: SyntheticTestCoinProvider):
        self.primary = primary
        self.supplemental = supplemental
        self.stale_after_seconds = primary.stale_after_seconds
        self.symbols = tuple(primary.symbols) + supplemental.symbols

    async def start(self) -> None:
        await self.primary.start()
        await self.supplemental.start()

    async def stop(self) -> None:
        await self.supplemental.stop()
        await self.primary.stop()

    def get_quotes(self) -> list[MarketQuote]:
        return self.primary.get_quotes() + self.supplemental.get_quotes()

    def get_quote(self, symbol: str) -> MarketQuote:
        if symbol.strip().upper() == TEST_SYMBOL:
            return self.supplemental.get_quote(symbol)
        return self.primary.get_quote(symbol)

    def get_trade_quote(self, symbol: str) -> MarketQuote:
        if symbol.strip().upper() == TEST_SYMBOL:
            return self.supplemental.get_trade_quote(symbol)
        return self.primary.get_trade_quote(symbol)

    def status(self) -> ProviderStatus:
        return self.primary.status()


def with_synthetic_test_coin(
    primary: MarketDataProvider, scanner: MarketScanner, *, enabled: bool
) -> MarketDataProvider:
    if not enabled:
        return primary
    return CompositeMarketDataProvider(primary, SyntheticTestCoinProvider(scanner))
