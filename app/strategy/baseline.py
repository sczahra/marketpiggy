from __future__ import annotations

from decimal import Decimal

from app.strategy.base import Decision, Strategy, StrategyConfig, StrategyInput


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

        fresh = [row for row in snapshot.rows if not row.quote.stale]
        if not fresh:
            return Decision("SKIP", None, "NO_FRESH_QUOTES", "No fresh executable quotes are available")
        eligible = [
            row for row in fresh
            if snapshot.observation_counts.get(row.quote.symbol, 0) >= config.minimum_observations
            and row.short_return_pct >= config.entry_momentum_pct
            and row.quote.spread_pct <= config.max_spread_pct
            and row.volatility_pct <= config.max_volatility_pct
        ]
        if not eligible:
            return Decision("HOLD", None, "NO_ENTRY", "No asset passes momentum, history, spread, and volatility rules")
        strongest = sorted(eligible, key=lambda row: (-row.short_return_pct, row.quote.symbol))[0]
        return Decision(
            "BUY", strongest.quote.symbol, "STRONGEST_MOMENTUM",
            f"{strongest.quote.symbol} has the strongest eligible momentum at {strongest.short_return_pct:.3f}%",
            self._signals(strongest),
        )
