from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from pathlib import Path
from threading import RLock
from uuid import uuid4

from app.broker.base import Broker


ZERO = Decimal("0")
QUANTITY_PLACES = Decimal("0.000000000000000001")
PRICE_PLACES = Decimal("0.000000000001")
MAX_STARTING_BALANCE = Decimal("1000000000")
ALL_SESSIONS = object()


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
    session_id: str | None = None
    provider_name: str | None = None
    quote_timestamp: str | None = None
    received_at: str | None = None
    session_number: int | None = None
    session_legacy: bool = False
    id: int | None = None

    @property
    def session_label(self) -> str:
        if self.session_id is None:
            return "Legacy"
        if self.session_legacy:
            return "Legacy session"
        return f"Simulation {self.session_number}"

    @property
    def provider_label(self) -> str:
        if self.session_id is None:
            return "Legacy"
        if self.provider_name == "Coinbase Advanced Trade":
            return "Live"
        if self.provider_name and self.provider_name.lower().startswith("static"):
            return "Static"
        return self.provider_name or "Unknown"

    @property
    def context_label(self) -> str:
        if self.session_id is None:
            return "Legacy"
        return f"{self.session_label} • {self.provider_label}"


@dataclass(frozen=True)
class SimulationSession:
    id: str
    number: int
    started_at: str
    starting_balance: Decimal
    legacy: bool = False

    @property
    def label(self) -> str:
        return "Legacy session" if self.legacy else f"Simulation {self.number}"


def _decimal(value: Decimal | str | int | float, field: str) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise OrderRejected(f"{field} must be a valid number") from exc
    if not result.is_finite():
        raise OrderRejected(f"{field} must be finite")
    return result


def _iso_timestamp(value: datetime | str | None) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


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
                CREATE TABLE IF NOT EXISTS simulation_resets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    previous_starting_balance TEXT NOT NULL,
                    new_starting_balance TEXT NOT NULL,
                    previous_cash TEXT NOT NULL,
                    previous_symbol TEXT,
                    previous_quantity TEXT NOT NULL,
                    previous_realized_pl TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS simulation_sessions (
                    id TEXT PRIMARY KEY,
                    sequence_number INTEGER NOT NULL UNIQUE,
                    started_at TEXT NOT NULL,
                    starting_balance TEXT NOT NULL,
                    legacy INTEGER NOT NULL DEFAULT 0
                );
                """
            )
            self._add_column(connection, "account", "current_session_id", "TEXT")
            self._add_column(connection, "trades", "session_id", "TEXT")
            self._add_column(connection, "trades", "provider_name", "TEXT")
            self._add_column(connection, "trades", "quote_timestamp", "TEXT")
            self._add_column(connection, "trades", "received_at", "TEXT")
            self._add_column(connection, "simulation_resets", "old_session_id", "TEXT")
            self._add_column(connection, "simulation_resets", "new_session_id", "TEXT")

            account = connection.execute("SELECT * FROM account WHERE id = 1").fetchone()
            if account is None:
                session = self._create_session(connection, self.starting_balance)
                connection.execute(
                    """
                    INSERT INTO account (
                        id, starting_balance, cash, symbol, quantity,
                        average_entry_price, realized_pl, current_session_id
                    ) VALUES (1, ?, ?, NULL, '0', '0', '0', ?)
                    """,
                    (str(self.starting_balance), str(self.starting_balance), session.id),
                )
            elif account["current_session_id"] is None:
                reset_count = connection.execute(
                    "SELECT COUNT(*) FROM simulation_resets"
                ).fetchone()[0]
                session = self._create_session(
                    connection,
                    Decimal(account["starting_balance"]),
                    legacy=True,
                    sequence_number=reset_count + 1,
                )
                connection.execute(
                    "UPDATE account SET current_session_id = ? WHERE id = 1",
                    (session.id,),
                )

    @staticmethod
    def _add_column(
        connection: sqlite3.Connection, table: str, column: str, definition: str
    ) -> None:
        columns = {
            row["name"] for row in connection.execute(f"PRAGMA table_info({table})")
        }
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @staticmethod
    def _create_session(
        connection: sqlite3.Connection,
        starting_balance: Decimal,
        *,
        legacy: bool = False,
        sequence_number: int | None = None,
    ) -> SimulationSession:
        if sequence_number is None:
            sequence_number = connection.execute(
                "SELECT COALESCE(MAX(sequence_number), 0) + 1 FROM simulation_sessions"
            ).fetchone()[0]
        session = SimulationSession(
            id=str(uuid4()),
            number=sequence_number,
            started_at=datetime.now(timezone.utc).isoformat(),
            starting_balance=starting_balance,
            legacy=legacy,
        )
        connection.execute(
            """
            INSERT INTO simulation_sessions (
                id, sequence_number, started_at, starting_balance, legacy
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                session.id,
                session.number,
                session.started_at,
                str(session.starting_balance),
                int(session.legacy),
            ),
        )
        return session

    def start_new_simulation(
        self, starting_balance: Decimal | str, *, preserve_history: bool
    ) -> Portfolio:
        """Reset account state after explicit confirmation, retaining prior trades."""
        balance = _decimal(starting_balance, "starting balance")
        if balance <= ZERO:
            raise OrderRejected("starting balance must be positive")
        if balance > MAX_STARTING_BALANCE:
            raise OrderRejected("starting balance must not exceed $1,000,000,000")
        if not preserve_history:
            raise OrderRejected("trade history must be preserved when resetting")

        with self._lock, self._connect() as connection:
            account = self._read_account(connection)
            new_session = self._create_session(connection, balance)
            connection.execute(
                """
                INSERT INTO simulation_resets (
                    timestamp, previous_starting_balance, new_starting_balance,
                    previous_cash, previous_symbol, previous_quantity,
                    previous_realized_pl, old_session_id, new_session_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.now(timezone.utc).isoformat(),
                    str(account["starting_balance"]),
                    str(balance),
                    str(account["cash"]),
                    account["symbol"],
                    str(account["quantity"]),
                    str(account["realized_pl"]),
                    account["current_session_id"],
                    new_session.id,
                ),
            )
            connection.execute(
                """
                UPDATE account SET starting_balance = ?, cash = ?, symbol = NULL,
                    quantity = '0', average_entry_price = '0', realized_pl = '0',
                    current_session_id = ?
                WHERE id = 1
                """,
                (str(balance), str(balance), new_session.id),
            )
            return self._portfolio_from_account(self._read_account(connection), None)

    def reset_count(self) -> int:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM simulation_resets"
            ).fetchone()
            return int(row["count"])

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
            "current_session_id": row["current_session_id"],
        }

    def active_session(self) -> SimulationSession:
        with self._lock, self._connect() as connection:
            account = self._read_account(connection)
            row = connection.execute(
                "SELECT * FROM simulation_sessions WHERE id = ?",
                (account["current_session_id"],),
            ).fetchone()
            return SimulationSession(
                id=row["id"],
                number=row["sequence_number"],
                started_at=row["started_at"],
                starting_balance=Decimal(row["starting_balance"]),
                legacy=bool(row["legacy"]),
            )

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
        provider_name: str | None = None,
        quote_timestamp: datetime | str | None = None,
        received_at: datetime | str | None = None,
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
                session_id=str(account["current_session_id"]),
                provider_name=provider_name or "Unknown",
                quote_timestamp=_iso_timestamp(quote_timestamp),
                received_at=_iso_timestamp(received_at),
            )
            return replace(trade, id=self._insert_trade(connection, trade))

    def sell(
        self,
        symbol: str,
        quantity: Decimal | str,
        *,
        bid: Decimal | str,
        ask: Decimal | str,
        provider_name: str | None = None,
        quote_timestamp: datetime | str | None = None,
        received_at: datetime | str | None = None,
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
                session_id=str(account["current_session_id"]),
                provider_name=provider_name or "Unknown",
                quote_timestamp=_iso_timestamp(quote_timestamp),
                received_at=_iso_timestamp(received_at),
            )
            return replace(trade, id=self._insert_trade(connection, trade))

    @staticmethod
    def _validate_quote(symbol: str, bid: Decimal, ask: Decimal) -> None:
        if not symbol:
            raise OrderRejected("symbol is required")
        if bid <= ZERO or ask <= ZERO or ask < bid:
            raise OrderRejected("invalid market quote")

    @staticmethod
    def _insert_trade(connection: sqlite3.Connection, trade: Trade) -> int:
        cursor = connection.execute(
            """
            INSERT INTO trades (
                timestamp, symbol, side, raw_bid, raw_ask, execution_price,
                quantity, friction_rate, friction_amount, resulting_cash,
                resulting_portfolio_value, session_id, provider_name,
                quote_timestamp, received_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trade.timestamp,
                trade.symbol,
                trade.side,
                str(trade.raw_bid),
                str(trade.raw_ask),
                str(trade.execution_price),
                str(trade.quantity),
                str(trade.friction_rate),
                str(trade.friction_amount),
                str(trade.resulting_cash),
                str(trade.resulting_portfolio_value),
                trade.session_id,
                trade.provider_name,
                trade.quote_timestamp,
                trade.received_at,
            ),
        )
        return int(cursor.lastrowid)

    def trades(
        self, limit: int = 100, session_id: str | None | object = ALL_SESSIONS
    ) -> list[Trade]:
        if limit <= 0:
            return []
        where = ""
        parameters: list[object] = []
        if session_id is None:
            where = "WHERE t.session_id IS NULL"
        elif session_id is not ALL_SESSIONS:
            where = "WHERE t.session_id = ?"
            parameters.append(session_id)
        parameters.append(limit)
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT t.*, s.sequence_number AS session_number,
                       s.legacy AS session_legacy
                FROM trades AS t
                LEFT JOIN simulation_sessions AS s ON s.id = t.session_id
                {where}
                ORDER BY t.id DESC LIMIT ?
                """,
                parameters,
            ).fetchall()
        return [self._row_to_trade(row) for row in rows]

    def trades_for_session(
        self, session_id: str | None, limit: int = 100
    ) -> list[Trade]:
        """Return only one session; None scopes explicitly to legacy rows."""
        return self.trades(limit=limit, session_id=session_id)

    @staticmethod
    def _row_to_trade(row: sqlite3.Row) -> Trade:
        return Trade(
            id=row["id"],
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
            session_id=row["session_id"],
            provider_name=row["provider_name"],
            quote_timestamp=row["quote_timestamp"],
            received_at=row["received_at"],
            session_number=row["session_number"],
            session_legacy=bool(row["session_legacy"]),
        )
