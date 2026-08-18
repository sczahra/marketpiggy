from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from pathlib import Path
from threading import RLock

from app.broker.base import Broker


ZERO = Decimal("0")
QUANTITY_PLACES = Decimal("0.000000000000000001")
PRICE_PLACES = Decimal("0.000000000001")


class OrderRejected(ValueError):
    """Raised when a paper order is invalid or cannot be funded."""


@dataclass(frozen=True)
class Portfolio:
    starting_balance: Decimal
    cash: Decimal
    symbol: str | None
    quantity: Decimal
    average_entry_price: Decimal
    mark_value: Decimal
    realized_pl: Decimal
    unrealized_pl: Decimal
    total_value: Decimal
    total_return_pct: Decimal


@dataclass(frozen=True)
class Trade:
    timestamp: str
    symbol: str
    side: str
    raw_bid: Decimal
    raw_ask: Decimal
    execution_price: Decimal
    quantity: Decimal
    friction_rate: Decimal
    friction_amount: Decimal
    resulting_cash: Decimal
    resulting_portfolio_value: Decimal


def _decimal(value: Decimal | str | int | float, field: str) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise OrderRejected(f"{field} must be a valid number") from exc
    if not result.is_finite():
        raise OrderRejected(f"{field} must be finite")
    return result


class PaperBroker(Broker):
    """Local-only, single-position paper broker backed by SQLite."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        starting_balance: Decimal | str = Decimal("10.00"),
        friction_rate: Decimal | str = Decimal("0.001"),
    ) -> None:
        self.database_path = Path(database_path)
        self.starting_balance = _decimal(starting_balance, "starting balance")
        self.friction_rate = _decimal(friction_rate, "friction rate")
        if self.starting_balance <= ZERO:
            raise ValueError("starting balance must be positive")
        if self.friction_rate < ZERO or self.friction_rate >= Decimal("1"):
            raise ValueError("friction rate must be between 0 and 1")

        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._initialize_database()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize_database(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS account (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    starting_balance TEXT NOT NULL,
                    cash TEXT NOT NULL,
                    symbol TEXT,
                    quantity TEXT NOT NULL,
                    average_entry_price TEXT NOT NULL,
                    realized_pl TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
                    raw_bid TEXT NOT NULL,
                    raw_ask TEXT NOT NULL,
                    execution_price TEXT NOT NULL,
                    quantity TEXT NOT NULL,
                    friction_rate TEXT NOT NULL,
                    friction_amount TEXT NOT NULL,
                    resulting_cash TEXT NOT NULL,
                    resulting_portfolio_value TEXT NOT NULL
                );
                """
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO account
                    (id, starting_balance, cash, symbol, quantity,
                     average_entry_price, realized_pl)
                VALUES (1, ?, ?, NULL, '0', '0', '0')
                """,
                (str(self.starting_balance), str(self.starting_balance)),
            )

    @staticmethod
    def _read_account(connection: sqlite3.Connection) -> dict[str, Decimal | str | None]:
        row = connection.execute("SELECT * FROM account WHERE id = 1").fetchone()
        return {
            "starting_balance": Decimal(row["starting_balance"]),
            "cash": Decimal(row["cash"]),
            "symbol": row["symbol"],
            "quantity": Decimal(row["quantity"]),
            "average_entry_price": Decimal(row["average_entry_price"]),
            "realized_pl": Decimal(row["realized_pl"]),
        }

    @staticmethod
    def _portfolio_from_account(
        account: dict[str, Decimal | str | None], bid: Decimal | None
    ) -> Portfolio:
        quantity = account["quantity"]
        average_entry_price = account["average_entry_price"]
        cash = account["cash"]
        starting_balance = account["starting_balance"]
        realized_pl = account["realized_pl"]
        assert isinstance(quantity, Decimal)
        assert isinstance(average_entry_price, Decimal)
        assert isinstance(cash, Decimal)
        assert isinstance(starting_balance, Decimal)
        assert isinstance(realized_pl, Decimal)

        mark_value = quantity * bid if bid is not None and quantity > ZERO else ZERO
        unrealized_pl = (
            mark_value - (quantity * average_entry_price) if quantity > ZERO else ZERO
        )
        total_value = cash + mark_value
        total_return_pct = (
            ((total_value - starting_balance) / starting_balance) * Decimal("100")
            if starting_balance
            else ZERO
        )
        return Portfolio(
            starting_balance=starting_balance,
            cash=cash,
            symbol=account["symbol"] if isinstance(account["symbol"], str) else None,
            quantity=quantity,
            average_entry_price=average_entry_price,
            mark_value=mark_value,
            realized_pl=realized_pl,
            unrealized_pl=unrealized_pl,
            total_value=total_value,
            total_return_pct=total_return_pct,
        )

    def portfolio(self, bid: Decimal | None = None) -> Portfolio:
        mark_bid = _decimal(bid, "bid") if bid is not None else None
        with self._lock, self._connect() as connection:
            return self._portfolio_from_account(self._read_account(connection), mark_bid)

    def buy(
        self,
        symbol: str,
        dollars: Decimal | str,
        *,
        bid: Decimal | str,
        ask: Decimal | str,
    ) -> Trade:
        symbol = symbol.strip().upper()
        dollars = _decimal(dollars, "dollar amount")
        bid = _decimal(bid, "bid")
        ask = _decimal(ask, "ask")
        self._validate_quote(symbol, bid, ask)
        if dollars <= ZERO:
            raise OrderRejected("buy amount must be positive")

        execution_price = (ask * (Decimal("1") + self.friction_rate)).quantize(PRICE_PLACES)
        friction_amount = execution_price - ask
        # Round down so representation precision can never make the order cost
        # fractionally more than the requested cash amount.
        quantity = (dollars / execution_price).quantize(
            QUANTITY_PLACES, rounding=ROUND_DOWN
        )
        cost = quantity * execution_price
        if quantity <= ZERO:
            raise OrderRejected("buy amount is too small")

        with self._lock, self._connect() as connection:
            account = self._read_account(connection)
            if account["symbol"] not in (None, symbol):
                raise OrderRejected(
                    f"close the existing {account['symbol']} position before buying {symbol}"
                )
            cash = account["cash"]
            old_quantity = account["quantity"]
            old_average = account["average_entry_price"]
            assert isinstance(cash, Decimal)
            assert isinstance(old_quantity, Decimal)
            assert isinstance(old_average, Decimal)
            if dollars > cash or cost > cash:
                raise OrderRejected("insufficient cash")

            new_quantity = old_quantity + quantity
            new_average = (
                ((old_quantity * old_average) + cost) / new_quantity
            ).quantize(PRICE_PLACES)
            new_cash = cash - cost
            portfolio_value = new_cash + (new_quantity * bid)
            connection.execute(
                """
                UPDATE account SET cash = ?, symbol = ?, quantity = ?,
                    average_entry_price = ? WHERE id = 1
                """,
                (str(new_cash), symbol, str(new_quantity), str(new_average)),
            )
            trade = Trade(
                timestamp=datetime.now(timezone.utc).isoformat(),
                symbol=symbol,
                side="BUY",
                raw_bid=bid,
                raw_ask=ask,
                execution_price=execution_price,
                quantity=quantity,
                friction_rate=self.friction_rate,
                friction_amount=friction_amount,
                resulting_cash=new_cash,
                resulting_portfolio_value=portfolio_value,
            )
            self._insert_trade(connection, trade)
            return trade

    def sell(
        self,
        symbol: str,
        quantity: Decimal | str,
        *,
        bid: Decimal | str,
        ask: Decimal | str,
    ) -> Trade:
        symbol = symbol.strip().upper()
        quantity = _decimal(quantity, "quantity")
        bid = _decimal(bid, "bid")
        ask = _decimal(ask, "ask")
        self._validate_quote(symbol, bid, ask)
        if quantity <= ZERO:
            raise OrderRejected("sell quantity must be positive")

        execution_price = (bid * (Decimal("1") - self.friction_rate)).quantize(PRICE_PLACES)
        friction_amount = bid - execution_price
        with self._lock, self._connect() as connection:
            account = self._read_account(connection)
            held_quantity = account["quantity"]
            average_entry_price = account["average_entry_price"]
            cash = account["cash"]
            realized_pl = account["realized_pl"]
            assert isinstance(held_quantity, Decimal)
            assert isinstance(average_entry_price, Decimal)
            assert isinstance(cash, Decimal)
            assert isinstance(realized_pl, Decimal)
            if account["symbol"] != symbol or held_quantity <= ZERO:
                raise OrderRejected(f"no {symbol} position is held")
            if quantity > held_quantity:
                raise OrderRejected("cannot sell more than the held quantity")

            proceeds = quantity * execution_price
            new_cash = cash + proceeds
            new_realized = realized_pl + quantity * (execution_price - average_entry_price)
            remaining = held_quantity - quantity
            new_symbol = symbol if remaining > ZERO else None
            new_average = average_entry_price if remaining > ZERO else ZERO
            portfolio_value = new_cash + (remaining * bid)
            connection.execute(
                """
                UPDATE account SET cash = ?, symbol = ?, quantity = ?,
                    average_entry_price = ?, realized_pl = ? WHERE id = 1
                """,
                (
                    str(new_cash),
                    new_symbol,
                    str(remaining),
                    str(new_average),
                    str(new_realized),
                ),
            )
            trade = Trade(
                timestamp=datetime.now(timezone.utc).isoformat(),
                symbol=symbol,
                side="SELL",
                raw_bid=bid,
                raw_ask=ask,
                execution_price=execution_price,
                quantity=quantity,
                friction_rate=self.friction_rate,
                friction_amount=friction_amount,
                resulting_cash=new_cash,
                resulting_portfolio_value=portfolio_value,
            )
            self._insert_trade(connection, trade)
            return trade

    @staticmethod
    def _validate_quote(symbol: str, bid: Decimal, ask: Decimal) -> None:
        if not symbol:
            raise OrderRejected("symbol is required")
        if bid <= ZERO or ask <= ZERO or ask < bid:
            raise OrderRejected("invalid market quote")

    @staticmethod
    def _insert_trade(connection: sqlite3.Connection, trade: Trade) -> None:
        connection.execute(
            """
            INSERT INTO trades (
                timestamp, symbol, side, raw_bid, raw_ask, execution_price,
                quantity, friction_rate, friction_amount, resulting_cash,
                resulting_portfolio_value
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            tuple(str(value) for value in trade.__dict__.values()),
        )

    def trades(self, limit: int = 100) -> list[Trade]:
        if limit <= 0:
            return []
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [
            Trade(
                timestamp=row["timestamp"],
                symbol=row["symbol"],
                side=row["side"],
                raw_bid=Decimal(row["raw_bid"]),
                raw_ask=Decimal(row["raw_ask"]),
                execution_price=Decimal(row["execution_price"]),
                quantity=Decimal(row["quantity"]),
                friction_rate=Decimal(row["friction_rate"]),
                friction_amount=Decimal(row["friction_amount"]),
                resulting_cash=Decimal(row["resulting_cash"]),
                resulting_portfolio_value=Decimal(row["resulting_portfolio_value"]),
            )
            for row in rows
        ]
