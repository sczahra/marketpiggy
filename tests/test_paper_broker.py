import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.broker.paper import OrderRejected, PaperBroker


@pytest.fixture
def broker(tmp_path):
    return PaperBroker(tmp_path / "test.db", friction_rate="0.01")


def test_buy_uses_ask_and_adverse_slippage(broker):
    trade = broker.buy("BTC", "5", bid="99", ask="100")
    assert trade.execution_price == Decimal("101.000000000000")
    assert trade.execution_price > trade.raw_ask
    assert trade.quantity == Decimal("0.049504950495049504")


def test_sell_uses_bid_and_adverse_slippage(broker):
    bought = broker.buy("BTC", "5", bid="99", ask="100")
    trade = broker.sell("BTC", bought.quantity, bid="110", ask="111")
    assert trade.execution_price == Decimal("108.900000000000")
    assert trade.execution_price < trade.raw_bid


def test_cash_cannot_go_negative(broker):
    with pytest.raises(OrderRejected, match="insufficient cash"):
        broker.buy("BTC", "11", bid="99", ask="100")
    assert broker.portfolio().cash == Decimal("10.00")


def test_entire_cash_balance_can_be_invested_without_rounding_overdraft(broker):
    broker.buy("BTC", "10", bid="99", ask="100")
    assert broker.portfolio().cash >= 0


def test_cannot_oversell(broker):
    bought = broker.buy("BTC", "5", bid="99", ask="100")
    with pytest.raises(OrderRejected, match="more than"):
        broker.sell("BTC", bought.quantity + Decimal("1"), bid="100", ask="101")


def test_only_one_asset_position_at_a_time(broker):
    broker.buy("BTC", "5", bid="99", ask="100")
    with pytest.raises(OrderRejected, match="existing BTC"):
        broker.buy("ETH", "1", bid="9", ask="10")


def test_realized_profit_and_full_close(broker):
    bought = broker.buy("BTC", "5", bid="99", ask="100")
    broker.sell("BTC", bought.quantity, bid="120", ask="121")
    portfolio = broker.portfolio()
    expected = bought.quantity * (Decimal("118.800000000000") - Decimal("101.000000000000"))
    assert portfolio.realized_pl == expected
    assert portfolio.symbol is None
    assert portfolio.quantity == 0


def test_unrealized_profit_uses_bid_mark(broker):
    trade = broker.buy("BTC", "5", bid="99", ask="100")
    portfolio = broker.portfolio(Decimal("110"))
    assert portfolio.mark_value == trade.quantity * Decimal("110")
    assert portfolio.unrealized_pl == trade.quantity * (Decimal("110") - trade.execution_price)


def test_portfolio_and_trades_survive_reload(tmp_path):
    path = tmp_path / "persistent.db"
    first = PaperBroker(path, friction_rate="0.01")
    trade = first.buy("BTC", "5", bid="99", ask="100")
    reloaded = PaperBroker(path, starting_balance="999", friction_rate="0.01")
    portfolio = reloaded.portfolio(Decimal("99"))
    assert portfolio.starting_balance == Decimal("10.00")
    assert portfolio.symbol == "BTC"
    assert portfolio.quantity == trade.quantity
    assert len(reloaded.trades()) == 1


def test_new_simulation_persists_non_default_balance_and_preserves_history(tmp_path):
    path = tmp_path / "reset.db"
    original = PaperBroker(path)
    original.buy("BTC", "5", bid="99", ask="100")

    reset = original.start_new_simulation("250.75", preserve_history=True)
    assert reset.starting_balance == Decimal("250.75")
    assert reset.cash == Decimal("250.75")
    assert reset.symbol is None
    assert reset.quantity == 0
    assert reset.realized_pl == 0
    assert len(original.trades()) == 1
    assert original.reset_count() == 1

    reloaded = PaperBroker(path)
    assert reloaded.portfolio().starting_balance == Decimal("250.75")
    assert reloaded.portfolio().cash == Decimal("250.75")
    assert len(reloaded.trades()) == 1
    assert reloaded.reset_count() == 1


def test_reset_creates_new_session_and_scoped_history_does_not_mix(tmp_path):
    broker = PaperBroker(tmp_path / "sessions.db")
    first_session = broker.active_session()
    first_trade = broker.buy(
        "BTC",
        "5",
        bid="99",
        ask="100",
        provider_name="Static simulation",
        quote_timestamp="2026-08-18T12:00:00+00:00",
        received_at="2026-08-18T12:00:01+00:00",
    )
    broker.start_new_simulation("25", preserve_history=True)
    second_session = broker.active_session()
    second_trade = broker.buy(
        "ETH",
        "5",
        bid="9",
        ask="10",
        provider_name="Coinbase Advanced Trade",
        quote_timestamp="2026-08-18T13:00:00+00:00",
        received_at="2026-08-18T13:00:00.100000+00:00",
    )

    assert first_session.id != second_session.id
    assert (first_session.number, second_session.number) == (1, 2)
    assert first_trade.session_id == first_session.id
    assert second_trade.session_id == second_session.id
    assert len(broker.trades()) == 2
    assert [trade.symbol for trade in broker.trades_for_session(first_session.id)] == ["BTC"]
    assert [trade.symbol for trade in broker.trades_for_session(second_session.id)] == ["ETH"]
    with sqlite3.connect(tmp_path / "sessions.db") as connection:
        reset_link = connection.execute(
            "SELECT old_session_id, new_session_id FROM simulation_resets"
        ).fetchone()
        assert reset_link == (first_session.id, second_session.id)


def test_trade_persists_provider_and_quote_timestamps(tmp_path):
    path = tmp_path / "provenance.db"
    broker = PaperBroker(path)
    quote_time = datetime(2026, 8, 18, 14, 0, tzinfo=timezone.utc)
    receive_time = datetime(2026, 8, 18, 14, 0, 0, 250000, tzinfo=timezone.utc)
    broker.buy(
        "BTC",
        "5",
        bid="99",
        ask="100",
        provider_name="Coinbase Advanced Trade",
        quote_timestamp=quote_time,
        received_at=receive_time,
    )

    loaded = PaperBroker(path).trades()[0]
    assert loaded.session_id == broker.active_session().id
    assert loaded.provider_name == "Coinbase Advanced Trade"
    assert loaded.quote_timestamp == quote_time.isoformat()
    assert loaded.received_at == receive_time.isoformat()
    assert loaded.context_label == "Simulation 1 • Live"


def test_milestone_2_database_migration_preserves_and_labels_legacy_rows(tmp_path):
    path = tmp_path / "milestone2.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE account (
                id INTEGER PRIMARY KEY, starting_balance TEXT NOT NULL,
                cash TEXT NOT NULL, symbol TEXT, quantity TEXT NOT NULL,
                average_entry_price TEXT NOT NULL, realized_pl TEXT NOT NULL
            );
            INSERT INTO account VALUES (1, '10.00', '4.50', 'BTC', '0.00005', '110000', '-0.25');
            CREATE TABLE trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
                symbol TEXT NOT NULL, side TEXT NOT NULL, raw_bid TEXT NOT NULL,
                raw_ask TEXT NOT NULL, execution_price TEXT NOT NULL,
                quantity TEXT NOT NULL, friction_rate TEXT NOT NULL,
                friction_amount TEXT NOT NULL, resulting_cash TEXT NOT NULL,
                resulting_portfolio_value TEXT NOT NULL
            );
            INSERT INTO trades VALUES (
                1, '2026-01-01T00:00:00+00:00', 'BTC', 'BUY', '109999',
                '110000', '110110', '0.00005', '0.001', '110', '4.50', '9.99'
            );
            CREATE TABLE simulation_resets (
                id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
                previous_starting_balance TEXT NOT NULL,
                new_starting_balance TEXT NOT NULL, previous_cash TEXT NOT NULL,
                previous_symbol TEXT, previous_quantity TEXT NOT NULL,
                previous_realized_pl TEXT NOT NULL
            );
            INSERT INTO simulation_resets VALUES (
                1, '2026-01-02T00:00:00+00:00', '5', '10', '5', NULL, '0', '0'
            );
            """
        )

    broker = PaperBroker(path)
    trades = broker.trades()
    assert len(trades) == 1
    assert trades[0].execution_price == Decimal("110110")
    assert trades[0].session_id is None
    assert trades[0].provider_name is None
    assert trades[0].context_label == "Legacy"
    assert broker.trades_for_session(None)[0].symbol == "BTC"
    migrated_session = broker.active_session()
    assert migrated_session.legacy is True
    assert PaperBroker(path).active_session().id == migrated_session.id
    with sqlite3.connect(path) as connection:
        reset = connection.execute(
            "SELECT old_session_id, new_session_id FROM simulation_resets WHERE id = 1"
        ).fetchone()
        assert reset == (None, None)
        assert connection.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM simulation_sessions").fetchone()[0] == 1


@pytest.mark.parametrize("balance", ["0", "-10", "nan", "1000000000.01"])
def test_invalid_new_simulation_balances_are_rejected(broker, balance):
    with pytest.raises(OrderRejected):
        broker.start_new_simulation(balance, preserve_history=True)


@pytest.mark.parametrize("amount", ["0", "-1", "nan", "Infinity"])
def test_invalid_buy_amounts_are_rejected(broker, amount):
    with pytest.raises(OrderRejected):
        broker.buy("BTC", amount, bid="99", ask="100")
