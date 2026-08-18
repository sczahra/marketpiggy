from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.broker.paper import OrderRejected, PaperBroker, Trade
from app.market_data.base import MarketDataError, MarketDataProvider
from app.market_data.scanner import MarketScanner
from app.strategy.base import Decision, Strategy, StrategyInput
from app.strategy.store import DecisionStore


LOGGER = logging.getLogger(__name__)


class StrategyRunner:
    """Lifecycle and safety boundary between one strategy and the paper broker."""

    def __init__(self, strategy: Strategy, provider: MarketDataProvider,
                 scanner: MarketScanner, broker: PaperBroker, store: DecisionStore) -> None:
        self.strategy = strategy
        self.provider = provider
        self.scanner = scanner
        self.broker = broker
        self.store = store
        self.enabled = False
        self.state = "Off"
        self.last_decision: Decision | None = None
        self.last_evaluated_at: datetime | None = None
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._wake_event = asyncio.Event()

    async def start(self) -> None:
        if self._task is None:
            self._stop_event.clear()
            self._task = asyncio.create_task(self._run(), name="paper-strategy-runner")

    async def stop(self) -> None:
        self.enabled = False
        self.state = "Off"
        self._stop_event.set()
        self._wake_event.set()
        if self._task is not None:
            await self._task
            self._task = None

    def enable(self) -> None:
        self.enabled = True
        self.state = "Watching"
        self._wake_event.set()

    def disable(self) -> None:
        self.enabled = False
        self.state = "Off"
        self._wake_event.set()

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            if not self.enabled:
                self._wake_event.clear()
                await self._wake_event.wait()
                continue
            try:
                self.evaluate_once()
            except Exception:
                LOGGER.exception("Autopilot evaluation failed")
                self.state = "Blocked"
            self._wake_event.clear()
            try:
                await asyncio.wait_for(
                    self._wake_event.wait(),
                    timeout=self.strategy.config.evaluation_interval_seconds,
                )
            except asyncio.TimeoutError:
                pass

    def _session_trades(self) -> list[Trade]:
        return self.broker.trades_for_session(self.broker.active_session().id, limit=1000)

    def _position_opened_at(self, symbol: str | None, trades: list[Trade]) -> datetime | None:
        if not symbol:
            return None
        buys: list[datetime] = []
        for trade in trades:  # newest first
            if trade.symbol != symbol:
                continue
            if trade.side == "SELL":
                break
            if trade.side == "BUY":
                buys.append(datetime.fromisoformat(trade.timestamp))
        return min(buys) if buys else None

    def _cooldown_until(self, trades: list[Trade]) -> datetime | None:
        for trade in trades:
            if trade.side == "SELL":
                return datetime.fromisoformat(trade.timestamp) + timedelta(
                    seconds=self.strategy.config.cooldown_seconds
                )
        return None

    def evaluate_once(self, *, now: datetime | None = None) -> Decision:
        if not self.enabled:
            self.state = "Off"
            return Decision("SKIP", None, "AUTOPILOT_OFF", "Autopilot is off")
        now = now or datetime.now(timezone.utc)
        self.last_evaluated_at = now
        session = self.broker.active_session()
        try:
            rows = tuple(self.scanner.rows(self.provider.get_quotes()))
        except Exception as exc:
            decision = Decision("SKIP", None, "MARKET_DATA_ERROR", f"Market data unavailable: {exc}")
            self._finish(session.id, decision, now=now)
            return decision

        trades = self._session_trades()
        unmarked = self.broker.portfolio()
        held_row = next((row for row in rows if row.quote.symbol == unmarked.symbol), None)
        portfolio = self.broker.portfolio(held_row.quote.bid if held_row else None)
        snapshot = StrategyInput(
            now=now,
            rows=rows,
            observation_counts={row.quote.symbol: self.scanner.observation_count(row.quote.symbol) for row in rows},
            portfolio=portfolio,
            position_opened_at=self._position_opened_at(portfolio.symbol, trades),
            cooldown_until=self._cooldown_until(trades) if not portfolio.symbol else None,
        )
        decision = self.strategy.decide(snapshot)
        if decision.action not in ("BUY", "SELL"):
            self._finish(session.id, decision, now=now)
            return decision

        if not self.enabled:  # Defensive check immediately before any execution.
            decision = Decision("SKIP", decision.symbol, "DISABLED", "Autopilot was disabled before execution", decision.signals)
            self._finish(session.id, decision, now=now)
            return decision
        try:
            assert decision.symbol is not None
            quote = self.provider.get_trade_quote(decision.symbol)
            if decision.action == "BUY":
                if quote.spread_pct > self.strategy.config.max_spread_pct:
                    raise OrderRejected("spread widened beyond the configured entry limit")
                current = self.broker.portfolio()
                if current.symbol or current.cash <= 0:
                    raise OrderRejected("account is no longer eligible for a new position")
                dollars = current.cash * self.strategy.config.position_size_fraction
                trade = self.broker.buy(
                    decision.symbol, dollars, bid=quote.bid, ask=quote.ask,
                    provider_name=quote.provider, quote_timestamp=quote.timestamp,
                    received_at=quote.received_at,
                )
            else:
                current = self.broker.portfolio()
                if current.symbol != decision.symbol or current.quantity <= 0:
                    raise OrderRejected("position changed before the exit could execute")
                trade = self.broker.sell(
                    decision.symbol, current.quantity, bid=quote.bid, ask=quote.ask,
                    provider_name=quote.provider, quote_timestamp=quote.timestamp,
                    received_at=quote.received_at,
                )
            self._finish(session.id, decision, executed=True, trade_id=trade.id, now=now)
        except (MarketDataError, OrderRejected) as exc:
            blocked = Decision("SKIP", decision.symbol, "EXECUTION_BLOCKED", str(exc), decision.signals)
            self._finish(session.id, blocked, now=now)
            return blocked
        return decision

    def _finish(self, session_id: str, decision: Decision, *, executed: bool = False,
                trade_id: int | None = None, now: datetime) -> None:
        self.last_decision = decision
        blocked_codes = {"NO_FRESH_QUOTES", "STALE_HELD_QUOTE", "MARKET_DATA_ERROR", "EXECUTION_BLOCKED"}
        if decision.reason_code == "COOLDOWN" or (executed and decision.action == "SELL"):
            self.state = "Cooldown"
        elif decision.reason_code in blocked_codes:
            self.state = "Blocked"
        elif self.broker.portfolio().symbol:
            self.state = "Holding"
        else:
            self.state = "Watching"
        self.store.record(session_id, self.strategy.name, self.strategy.version,
                          decision, executed=executed, trade_id=trade_id, now=now)

    def status(self) -> dict[str, object]:
        decision = self.last_decision
        return {
            "enabled": self.enabled,
            "state": self.state,
            "strategy": self.strategy.name,
            "version": self.strategy.version,
            "last_action": decision.action if decision else None,
            "last_symbol": decision.symbol if decision else None,
            "last_reason_code": decision.reason_code if decision else None,
            "last_reason": decision.reason if decision else "Autopilot has not evaluated this run",
            "last_evaluated_at": self.last_evaluated_at.isoformat() if self.last_evaluated_at else None,
        }
