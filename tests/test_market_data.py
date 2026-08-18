import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.market_data.base import MarketDataError, MarketQuote
from app.market_data.coinbase_provider import CoinbaseProvider
from app.market_data.observations import ObservationStore
from app.market_data.scanner import MarketScanner
from app.market_data.static_provider import StaticMarketDataProvider


NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)


def ticker_message(product_id="BTC-USD", bid="100.10", ask="100.20"):
    return {
        "channel": "ticker",
        "timestamp": "2026-08-18T12:00:00Z",
        "events": [
            {
                "type": "update",
                "tickers": [
                    {
                        "type": "ticker",
                        "product_id": product_id,
                        "best_bid": bid,
                        "best_ask": ask,
                    }
                ],
            }
        ],
    }


def quote(symbol, midpoint, received_at=NOW, provider="test"):
    midpoint = Decimal(str(midpoint))
    return MarketQuote(
        symbol=symbol,
        bid=midpoint - Decimal("0.01"),
        ask=midpoint + Decimal("0.01"),
        timestamp=received_at,
        received_at=received_at,
        provider=provider,
    )


def test_coinbase_ticker_parses_best_bid_and_ask():
    provider = CoinbaseProvider(symbols=("BTC",))
    parsed = provider.parse_message(ticker_message(), received_at=NOW)
    assert len(parsed) == 1
    assert parsed[0].symbol == "BTC"
    assert parsed[0].bid == Decimal("100.10")
    assert parsed[0].ask == Decimal("100.20")
    assert parsed[0].provider == "Coinbase Advanced Trade"


def test_coinbase_symbol_mapping_and_unavailable_product_handling():
    provider = CoinbaseProvider(symbols=("BTC", "ETH"))
    assert provider.product_ids == ("BTC-USD", "ETH-USD")
    assert provider.parse_message(ticker_message("SOL-USD"), received_at=NOW) == []
    with pytest.raises(MarketDataError, match="not configured"):
        provider.get_quote("SOL")


@pytest.mark.parametrize("payload", ["not json", {}, {"channel": "heartbeats"}, {"channel": "ticker", "events": "bad"}])
def test_malformed_and_unsupported_messages_are_ignored(payload):
    provider = CoinbaseProvider(symbols=("BTC",))
    assert provider.handle_message(payload, received_at=NOW) == 0
    assert provider.get_quotes() == []


def test_stale_quote_detection_and_trade_rejection(monkeypatch):
    provider = CoinbaseProvider(symbols=("BTC",), stale_after_seconds=15)
    provider._connected = True
    provider.handle_message(ticker_message(), received_at=NOW - timedelta(seconds=20))
    monkeypatch.setattr(
        "app.market_data.base.datetime",
        type("Clock", (), {"now": staticmethod(lambda tz=None: NOW)}),
    )
    stale = provider.get_quote("BTC")
    assert stale.stale is True
    assert stale.age_seconds == 20
    with pytest.raises(MarketDataError, match="paper order rejected"):
        provider.get_trade_quote("BTC")


def test_disconnect_immediately_marks_recent_quote_unsafe():
    provider = CoinbaseProvider(symbols=("BTC",), stale_after_seconds=15)
    provider._connected = True
    provider.handle_message(ticker_message(), received_at=datetime.now(timezone.utc))
    assert provider.get_quote("BTC").stale is False
    provider._mark_disconnected(ConnectionError("socket lost"), attempts=2)
    assert provider.get_quote("BTC").stale is True
    status = provider.status()
    assert status.connected is False
    assert status.error == "socket lost"
    assert status.reconnect_attempts == 2


def test_reconnect_backoff_is_bounded():
    assert [CoinbaseProvider.reconnect_delay(i) for i in range(1, 8)] == [
        1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0
    ]


def test_provider_background_task_stops_cleanly():
    class WaitingProvider(CoinbaseProvider):
        async def _connection_loop(self):
            await self._stop_event.wait()

    async def exercise_lifecycle():
        provider = WaitingProvider(symbols=("BTC",))
        await provider.start()
        assert provider._task is not None
        await provider.stop()
        assert provider._task is None
        assert provider.status().connected is False

    asyncio.run(exercise_lifecycle())


def test_static_provider_remains_fresh_and_labeled_static():
    provider = StaticMarketDataProvider()
    quote = provider.get_quote("BTC")
    assert quote.bid < quote.ask
    assert quote.stale is False
    assert provider.status().connected is True
    assert provider.status().live is False


def test_scanner_return_and_volatility_calculations():
    scanner = MarketScanner(window_seconds=60)
    scanner.record(quote("BTC", 100, NOW))
    scanner.record(quote("BTC", 110, NOW + timedelta(seconds=1)))
    scanner.record(quote("BTC", 99, NOW + timedelta(seconds=2)))
    short_return, volatility = scanner.metrics("BTC")
    assert short_return == Decimal("-1.00")
    assert volatility == Decimal("10.0")


def test_observation_persistence_is_sampled_at_most_once_per_interval(tmp_path):
    store = ObservationStore(tmp_path / "observations.db", sample_interval_seconds=1)
    quotes = [quote("BTC", 100), quote("ETH", 10)]
    assert store.sample_if_due(quotes, NOW) == 2
    assert store.sample_if_due(quotes, NOW + timedelta(milliseconds=999)) == 0
    assert store.sample_if_due(quotes, NOW + timedelta(seconds=1)) == 2
    assert store.count() == 4
