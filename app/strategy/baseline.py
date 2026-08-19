from __future__ import annotations

from decimal import Decimal

from app.strategy.base import (
    Decision,
    EntryEvaluation,
    Strategy,
    StrategyConfig,
    StrategyInput,
)


class BaselineMomentumStrategy(Strategy):
    """One deterministic, explainable momentum rule for paper trading only."""

    name = "Baseline Momentum"
    version = "1.0"

    def __init__(self, config: StrategyConfig | None = None) -> None:
        self.config = config or StrategyConfig()

    @staticmethod
    def _signals(row) -> dict[str, str]:
        return {
            "momentum_pct": str(row.short_return_pct),
            "spread_pct": str(row.quote.spread_pct),
            "volatility_pct": str(row.volatility_pct),
            "quote_age_seconds": str(round(row.quote.age_seconds, 3)),
        }

    def evaluate_entry(self, row, observation_count: int) -> EntryEvaluation:
        """Evaluate entry rules once for both trading and presentation."""
        quote = row.quote
        if quote.stale:
            return EntryEvaluation(quote.symbol, (), "Stale quote")
        if quote.bid <= 0 or quote.ask < quote.bid:
            return EntryEvaluation(quote.symbol, (), "Unusable quote")
        failures: list[str] = []
        if observation_count < self.config.minimum_observations:
            failures.append("HISTORY")
        if row.short_return_pct < self.config.entry_momentum_pct:
            failures.append("MOMENTUM")
        if quote.spread_pct > self.config.max_spread_pct:
            failures.append("SPREAD")
        if row.volatility_pct > self.config.max_volatility_pct:
            failures.append("VOLATILITY")
        return EntryEvaluation(quote.symbol, tuple(failures))

    def decide(self, snapshot: StrategyInput) -> Decision:
        config = self.config
        if snapshot.cooldown_until and snapshot.now < snapshot.cooldown_until:
            seconds = max(0, int((snapshot.cooldown_until - snapshot.now).total_seconds()))
            return Decision("SKIP", None, "COOLDOWN", f"Waiting {seconds}s after the last exit")

        if snapshot.portfolio.symbol:
            symbol = snapshot.portfolio.symbol
            row = next((item for item in snapshot.rows if item.quote.symbol == symbol), None)
            if row is None or row.quote.stale:
                return Decision("SKIP", symbol, "STALE_HELD_QUOTE", "Held asset has no fresh executable quote")
            signals = self._signals(row)
            bid_return = (
                ((row.quote.bid / snapshot.portfolio.average_entry_price) - Decimal("1"))
                * Decimal("100")
            )
            signals["position_return_pct"] = str(bid_return)
            if bid_return <= -config.stop_loss_pct:
                return Decision("SELL", symbol, "STOP_LOSS", f"Bid return {bid_return:.3f}% reached -{config.stop_loss_pct}% stop", signals)
            if bid_return >= config.take_profit_pct:
                return Decision("SELL", symbol, "TAKE_PROFIT", f"Bid return {bid_return:.3f}% reached +{config.take_profit_pct}% target", signals)
            if snapshot.position_opened_at:
                held_seconds = (snapshot.now - snapshot.position_opened_at).total_seconds()
                signals["holding_seconds"] = str(round(held_seconds, 3))
                if held_seconds >= config.max_holding_seconds:
                    return Decision("SELL", symbol, "MAX_HOLD", f"Position reached the {config.max_holding_seconds}s holding limit", signals)
            if row.short_return_pct <= config.reversal_momentum_pct:
                return Decision("SELL", symbol, "MOMENTUM_REVERSAL", f"Momentum {row.short_return_pct:.3f}% fell to the {config.reversal_momentum_pct}% reversal level", signals)
            return Decision("HOLD", symbol, "POSITION_HELD", "Position remains inside all exit limits", signals)

        evaluations = {
            row.quote.symbol: self.evaluate_entry(
                row, snapshot.observation_counts.get(row.quote.symbol, 0)
            )
            for row in snapshot.rows
        }
        safe = [
            row for row in snapshot.rows
            if evaluations[row.quote.symbol].safety_reason is None
        ]
        if not safe:
            return Decision("SKIP", None, "NO_FRESH_QUOTES", "No fresh executable quotes are available")
        eligible = [
            row for row in safe if evaluations[row.quote.symbol].eligible
        ]
        if not eligible:
            return Decision("HOLD", None, "NO_ENTRY", "No asset passes momentum, history, spread, and volatility rules")
        strongest = sorted(eligible, key=lambda row: (-row.short_return_pct, row.quote.symbol))[0]
        return Decision(
            "BUY", strongest.quote.symbol, "STRONGEST_MOMENTUM",
            f"{strongest.quote.symbol} has the strongest eligible momentum at {strongest.short_return_pct:.3f}%",
            self._signals(strongest),
        )
