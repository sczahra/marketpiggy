from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from threading import RLock
from typing import Any

from websockets.asyncio.client import connect

from app.market_data.base import (
    MarketDataError,
    MarketDataProvider,
    MarketQuote,
    ProviderStatus,
)
from app.market_data.scanner import MarketScanner


COINBASE_WEBSOCKET_URL = "wss://advanced-trade-ws.coinbase.com"
DEFAULT_SYMBOLS = ("BTC", "ETH", "SOL", "DOGE", "XRP")


def _parse_timestamp(value: object, fallback: datetime) -> datetime:
    if not isinstance(value, str):
        return fallback
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return fallback


class CoinbaseProvider(MarketDataProvider):
    """Unauthenticated Coinbase Advanced Trade ticker consumer."""

    name = "Coinbase Advanced Trade"
    live = True

    def __init__(
        self,
        *,
        symbols: tuple[str, ...] = DEFAULT_SYMBOLS,
        stale_after_seconds: float = 15.0,
        scanner: MarketScanner | None = None,
        websocket_url: str = COINBASE_WEBSOCKET_URL,
    ) -> None:
        if stale_after_seconds <= 0:
            raise ValueError("stale threshold must be positive")
        normalized = tuple(dict.fromkeys(symbol.strip().upper() for symbol in symbols))
        if not normalized or any(not symbol.isalnum() for symbol in normalized):
            raise ValueError("Coinbase symbols must be non-empty alphanumeric codes")
        self.symbols = normalized
        self.product_ids = tuple(f"{symbol}-USD" for symbol in normalized)
        self._product_to_symbol = dict(zip(self.product_ids, normalized))
        self.stale_after_seconds = stale_after_seconds
        self.scanner = scanner or MarketScanner()
        self.websocket_url = websocket_url
        self._quotes: dict[str, MarketQuote] = {}
        self._lock = RLock()
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._connected = False
        self._last_update: datetime | None = None
        self._last_error: str | None = None
        self._reconnect_attempts = 0

    @staticmethod
    def reconnect_delay(attempt: int) -> float:
        return float(min(30, 2 ** max(0, attempt - 1)))

    async def start(self) -> None:
        if self._task is None:
            self._stop_event.clear()
            self._task = asyncio.create_task(
                self._connection_loop(), name="coinbase-market-data"
            )

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self._connected = False

    async def _connection_loop(self) -> None:
        attempt = 0
        while not self._stop_event.is_set():
            try:
                await self._consume_connection()
                if self._stop_event.is_set():
                    break
                raise ConnectionError("Coinbase WebSocket closed")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._connected:
                    attempt = 0
                attempt += 1
                self._mark_disconnected(exc, attempt)
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=self.reconnect_delay(attempt)
                    )
                except TimeoutError:
                    pass

    async def _consume_connection(self) -> None:
        async with connect(
            self.websocket_url,
            open_timeout=10,
            close_timeout=5,
            ping_interval=20,
            ping_timeout=20,
        ) as websocket:
            await websocket.send(
                json.dumps(
                    {
                        "type": "subscribe",
                        "product_ids": list(self.product_ids),
                        "channel": "ticker",
                    }
                )
            )
            await websocket.send(json.dumps({"type": "subscribe", "channel": "heartbeats"}))
            self._connected = True
            self._last_error = None
            self._reconnect_attempts = 0
            async for message in websocket:
                self.handle_message(message)

    def _mark_disconnected(self, error: Exception, attempts: int = 1) -> None:
        self._connected = False
        self._last_error = str(error) or error.__class__.__name__
        self._reconnect_attempts = attempts

    def parse_message(
        self, raw_message: str | bytes | dict[str, Any], received_at: datetime | None = None
    ) -> list[MarketQuote]:
        received_at = received_at or datetime.now(timezone.utc)
        try:
            payload = (
                raw_message
                if isinstance(raw_message, dict)
                else json.loads(raw_message.decode() if isinstance(raw_message, bytes) else raw_message)
            )
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
            return []
        if not isinstance(payload, dict) or payload.get("channel") != "ticker":
            return []
        provider_timestamp = _parse_timestamp(payload.get("timestamp"), received_at)
        quotes: list[MarketQuote] = []
        events = payload.get("events")
        if not isinstance(events, list):
            return []
        for event in events:
            if not isinstance(event, dict) or not isinstance(event.get("tickers"), list):
                continue
            for ticker in event["tickers"]:
                if not isinstance(ticker, dict):
                    continue
                symbol = self._product_to_symbol.get(ticker.get("product_id"))
                if symbol is None:
                    continue
                try:
                    bid = Decimal(str(ticker["best_bid"]))
                    ask = Decimal(str(ticker["best_ask"]))
                except (KeyError, InvalidOperation, ValueError):
                    continue
                if not bid.is_finite() or not ask.is_finite() or bid <= 0 or ask < bid:
                    continue
                quotes.append(
                    MarketQuote(
                        symbol=symbol,
                        bid=bid,
                        ask=ask,
                        timestamp=provider_timestamp,
                        received_at=received_at,
                        provider=self.name,
                    )
                )
        return quotes

    def handle_message(
        self, raw_message: str | bytes | dict[str, Any], received_at: datetime | None = None
    ) -> int:
        quotes = self.parse_message(raw_message, received_at)
        if not quotes:
            return 0
        with self._lock:
            for quote in quotes:
                self._quotes[quote.symbol] = quote
                self.scanner.record(quote)
                self._last_update = quote.received_at
        return len(quotes)

    def _decorate(self, quote: MarketQuote) -> MarketQuote:
        return quote.with_freshness(
            connected=self._connected,
            stale_after_seconds=self.stale_after_seconds,
        )

    def get_quotes(self) -> list[MarketQuote]:
        with self._lock:
            return [
                self._decorate(self._quotes[symbol])
                for symbol in self.symbols
                if symbol in self._quotes
            ]

    def get_quote(self, symbol: str) -> MarketQuote:
        symbol = symbol.strip().upper()
        if symbol not in self.symbols:
            raise MarketDataError(f"{symbol} is not configured for Coinbase live data")
        with self._lock:
            quote = self._quotes.get(symbol)
            if quote is None:
                raise MarketDataError(f"No live {symbol} quote is available yet")
            return self._decorate(quote)

    def status(self) -> ProviderStatus:
        return ProviderStatus(
            name=self.name,
            live=True,
            connected=self._connected,
            last_update=self._last_update,
            error=self._last_error,
            reconnect_attempts=self._reconnect_attempts,
        )
