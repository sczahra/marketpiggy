from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock

from app.market_data.base import MarketDataProvider, MarketQuote


class ObservationStore:
    """Samples current quotes at a bounded cadence for future replay work."""

    def __init__(self, database_path: str | Path, sample_interval_seconds: float = 1.0):
        if sample_interval_seconds <= 0:
            raise ValueError("sample interval must be positive")
        self.database_path = Path(database_path)
        self.sample_interval_seconds = sample_interval_seconds
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._last_sample_at: datetime | None = None
        self._lock = RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS market_observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sampled_at TEXT NOT NULL,
                    quote_timestamp TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    bid TEXT NOT NULL,
                    ask TEXT NOT NULL,
                    midpoint TEXT NOT NULL,
                    spread TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_market_observations_symbol_time
                    ON market_observations(symbol, sampled_at);
                """
            )

    def sample_if_due(
        self, quotes: list[MarketQuote], sampled_at: datetime | None = None
    ) -> int:
        sampled_at = sampled_at or datetime.now(timezone.utc)
        with self._lock:
            if self._last_sample_at is not None:
                elapsed = (sampled_at - self._last_sample_at).total_seconds()
                if elapsed < self.sample_interval_seconds:
                    return 0
            self._last_sample_at = sampled_at
            if not quotes:
                return 0
            rows = [
                (
                    sampled_at.isoformat(),
                    quote.timestamp.isoformat(),
                    quote.provider,
                    quote.symbol,
                    str(quote.bid),
                    str(quote.ask),
                    str(quote.midpoint),
                    str(quote.spread),
                )
                for quote in quotes
            ]
            with self._connect() as connection:
                connection.executemany(
                    """
                    INSERT INTO market_observations (
                        sampled_at, quote_timestamp, provider, symbol,
                        bid, ask, midpoint, spread
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
            return len(rows)

    def count(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM market_observations"
            ).fetchone()
            return int(row["count"])


class ObservationSampler:
    def __init__(self, provider: MarketDataProvider, store: ObservationStore):
        self.provider = provider
        self.store = store
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="market-observation-sampler")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        while True:
            await asyncio.to_thread(self.store.sample_if_due, self.provider.get_quotes())
            await asyncio.sleep(self.store.sample_interval_seconds)
