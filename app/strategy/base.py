from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from math import isfinite

from app.broker.paper import Portfolio
from app.market_data.scanner import ScannerRow


def _finite(value: Decimal | str | float, name: str) -> Decimal:
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not number.is_finite():
        raise ValueError(f"{name} must be finite")
    return number


@dataclass(frozen=True)
class StrategyConfig:
    entry_momentum_pct: Decimal = Decimal("0.20")
    max_spread_pct: Decimal = Decimal("0.20")
    max_volatility_pct: Decimal = Decimal("0.35")
    position_size_fraction: Decimal = Decimal("0.25")
    stop_loss_pct: Decimal = Decimal("1.00")
    take_profit_pct: Decimal = Decimal("1.50")
    max_holding_seconds: int = 900
    reversal_momentum_pct: Decimal = Decimal("-0.10")
    evaluation_interval_seconds: float = 3.0
    cooldown_seconds: int = 60
    minimum_observations: int = 5

    def __post_init__(self) -> None:
        decimal_fields = (
            "entry_momentum_pct", "max_spread_pct", "max_volatility_pct",
            "position_size_fraction", "stop_loss_pct", "take_profit_pct",
            "reversal_momentum_pct",
        )
        for name in decimal_fields:
            object.__setattr__(self, name, _finite(getattr(self, name), name))
        if self.entry_momentum_pct <= 0:
            raise ValueError("entry_momentum_pct must be positive")
        if self.max_spread_pct <= 0 or self.max_volatility_pct <= 0:
            raise ValueError("spread and volatility limits must be positive")
        if not Decimal("0") < self.position_size_fraction <= Decimal("1"):
            raise ValueError("position_size_fraction must be greater than 0 and at most 1")
        if self.stop_loss_pct <= 0 or self.take_profit_pct <= 0:
            raise ValueError("stop and profit thresholds must be positive")
        if self.reversal_momentum_pct >= 0:
            raise ValueError("reversal_momentum_pct must be negative")
        if self.max_holding_seconds <= 0 or self.cooldown_seconds < 0:
            raise ValueError("holding time must be positive and cooldown cannot be negative")
        if not isfinite(self.evaluation_interval_seconds) or self.evaluation_interval_seconds < 1:
            raise ValueError("evaluation_interval_seconds must be at least 1")
        if self.minimum_observations < 2:
            raise ValueError("minimum_observations must be at least 2")


@dataclass(frozen=True)
class StrategyInput:
    now: datetime
    rows: tuple[ScannerRow, ...]
    observation_counts: dict[str, int]
    portfolio: Portfolio
    position_opened_at: datetime | None = None
    cooldown_until: datetime | None = None


@dataclass(frozen=True)
class Decision:
    action: str
    symbol: str | None
    reason_code: str
    reason: str
    signals: dict[str, str] = field(default_factory=dict)


ENTRY_RULE_LABELS = {
    "HISTORY": "Waiting for history",
    "MOMENTUM": "Momentum below threshold",
    "SPREAD": "Spread too wide",
    "VOLATILITY": "Volatility too high",
}


@dataclass(frozen=True)
class EntryEvaluation:
    """The single source of truth for strategy entry rules and UI lights."""

    symbol: str
    failed_rules: tuple[str, ...]
    safety_reason: str | None = None

    @property
    def eligible(self) -> bool:
        return self.safety_reason is None and not self.failed_rules

    @property
    def light(self) -> str:
        if self.safety_reason or len(self.failed_rules) > 1:
            return "red"
        if self.failed_rules:
            return "yellow"
        return "green"

    @property
    def explanation(self) -> str:
        if self.safety_reason:
            return self.safety_reason
        if not self.failed_rules:
            return "Eligible"
        if len(self.failed_rules) == 1:
            return ENTRY_RULE_LABELS[self.failed_rules[0]]
        return f"{len(self.failed_rules)} rules failing"


class Strategy(ABC):
    name: str
    version: str
    config: StrategyConfig

    @abstractmethod
    def decide(self, snapshot: StrategyInput) -> Decision:
        raise NotImplementedError
