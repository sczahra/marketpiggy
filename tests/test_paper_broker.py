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


@pytest.mark.parametrize("amount", ["0", "-1", "nan", "Infinity"])
def test_invalid_buy_amounts_are_rejected(broker, amount):
    with pytest.raises(OrderRejected):
        broker.buy("BTC", amount, bid="99", ask="100")
