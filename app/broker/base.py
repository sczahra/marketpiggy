from abc import ABC, abstractmethod
from decimal import Decimal


class Broker(ABC):
    """Boundary used by the web app for paper or future broker implementations."""

    @abstractmethod
    def buy(self, symbol: str, dollars: Decimal, *, bid: Decimal, ask: Decimal):
        raise NotImplementedError

    @abstractmethod
    def sell(self, symbol: str, quantity: Decimal, *, bid: Decimal, ask: Decimal):
        raise NotImplementedError

    @abstractmethod
    def portfolio(self, bid: Decimal | None = None):
        raise NotImplementedError
