from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.broker.paper import PaperBroker, Portfolio
from app.market_data.base import MarketDataError, MarketDataProvider, MarketQuote, ProviderStatus
from app.market_data.coinbase_provider import CoinbaseProvider
from app.market_data.scanner import MarketScanner, ScannerRow
from app.market_data.static_provider import StaticMarketDataProvider
from app.market_data.synthetic_test_coin import (
    CompositeMarketDataProvider,
    SyntheticTestCoinProvider,
    TEST_PROVIDER_NAME,
    test_coin_enabled as parse_test_coin_enabled,
    with_synthetic_test_coin,
)
from app.strategy import BaselineMomentumStrategy, DecisionStore, StrategyInput, StrategyRunner


NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)


def scanner_row(*, momentum="0.30", spread_pct="0.10", volatility="0.05", stale=False):
    midpoint = Decimal("100")
    half_spread = midpoint * Decimal(spread_pct) / Decimal("200")
    quote = MarketQuote(
        "BTC", midpoint - half_spread, midpoint + half_spread,
        NOW, NOW, "fixture", stale=stale,
    )
    return ScannerRow(quote, Decimal(momentum), Decimal(volatility))


def empty_portfolio():
    return Portfolio(
        Decimal("10"), Decimal("10"), None, Decimal("0"), Decimal("0"),
        Decimal("0"), Decimal("0"), Decimal("0"), Decimal("10"), Decimal("0"),
    )


def test_green_means_every_shared_entry_rule_passes():
    strategy = BaselineMomentumStrategy()
    evaluation = strategy.evaluate_entry(scanner_row(), 5)
    assert evaluation.eligible is True
    assert evaluation.light == "green"
    assert evaluation.explanation == "Eligible"


@pytest.mark.parametrize(
    ("row", "count", "explanation"),
    [
        (scanner_row(), 4, "Waiting for history"),
        (scanner_row(momentum="0.19"), 5, "Momentum below threshold"),
        (scanner_row(spread_pct="0.21"), 5, "Spread too wide"),
        (scanner_row(volatility="0.36"), 5, "Volatility too high"),
    ],
)
def test_yellow_means_exactly_one_normal_rule_fails(row, count, explanation):
    evaluation = BaselineMomentumStrategy().evaluate_entry(row, count)
    assert evaluation.light == "yellow"
    assert evaluation.explanation == explanation
    assert len(evaluation.failed_rules) == 1


def test_red_means_multiple_rules_fail_or_quote_is_stale():
    strategy = BaselineMomentumStrategy()
    multiple = strategy.evaluate_entry(scanner_row(momentum="0.10", spread_pct="0.30"), 4)
    stale = strategy.evaluate_entry(scanner_row(stale=True), 5)
    unusable_row = scanner_row()
    unusable_quote = MarketQuote(
        "BTC", Decimal("0"), Decimal("100"), NOW, NOW, "fixture"
    )
    unusable = strategy.evaluate_entry(
        ScannerRow(unusable_quote, unusable_row.short_return_pct,
                   unusable_row.volatility_pct),
        5,
    )
    assert multiple.light == "red"
    assert multiple.explanation == "3 rules failing"
    assert stale.light == "red"
    assert stale.explanation == "Stale quote"
    assert unusable.light == "red"
    assert unusable.explanation == "Unusable quote"


def test_strategy_selection_consumes_the_same_evaluation_as_the_light():
    strategy = BaselineMomentumStrategy()
    row = scanner_row()
    evaluation = strategy.evaluate_entry(row, 5)
    snapshot = StrategyInput(NOW, (row,), {"BTC": 5}, empty_portfolio())
    decision = strategy.decide(snapshot)
    assert evaluation.light == "green"
    assert evaluation.explanation == "Eligible"
    assert (decision.action, decision.symbol) == ("BUY", "BTC")


def test_test_coin_is_disabled_by_default_and_configuration_is_strict():
    scanner = MarketScanner()
    primary = StaticMarketDataProvider(scanner=scanner)
    assert parse_test_coin_enabled(None) is False
    assert parse_test_coin_enabled("0") is False
    assert parse_test_coin_enabled("1") is True
    assert with_synthetic_test_coin(primary, scanner, enabled=False) is primary
    with pytest.raises(ValueError, match="must be '0' or '1'"):
        parse_test_coin_enabled("true")


def test_enabling_test_preserves_primary_symbols_and_adds_only_synthetic_test():
    scanner = MarketScanner()
    primary = StaticMarketDataProvider(scanner=scanner)
    combined = with_synthetic_test_coin(primary, scanner, enabled=True)
    assert isinstance(combined, CompositeMarketDataProvider)
    assert combined.symbols == primary.symbols + ("TEST",)
    quotes = combined.get_quotes()
    assert {quote.symbol for quote in quotes} == set(primary.symbols) | {"TEST"}
    test_quote = next(quote for quote in quotes if quote.symbol == "TEST")
    assert test_quote.provider == TEST_PROVIDER_NAME
    assert test_quote.stale is False


def test_test_coin_naturally_progresses_red_yellow_green_under_normal_rules():
    scanner = MarketScanner()
    provider = SyntheticTestCoinProvider(scanner)
    strategy = BaselineMomentumStrategy()
    lights = []
    explanations = []
    for _ in range(5):
        quote = provider.get_quote("TEST")
        row = scanner.rows([quote])[0]
        evaluation = strategy.evaluate_entry(row, scanner.observation_count("TEST"))
        lights.append(evaluation.light)
        explanations.append(evaluation.explanation)
    assert lights == ["red", "yellow", "yellow", "yellow", "green"]
    assert explanations[0] == "2 rules failing"
    assert explanations[-1] == "Eligible"


def test_composition_preserves_primary_stale_quote_rejection():
    class StalePrimary(MarketDataProvider):
        symbols = ("BTC",)
        stale_after_seconds = 15

        def get_quotes(self):
            return [self.get_quote("BTC")]

        def get_quote(self, symbol):
            return MarketQuote(
                "BTC", Decimal("99"), Decimal("100"), NOW, NOW,
                "Live fixture", stale=True,
            )

        def status(self):
            return ProviderStatus("Live fixture", True, False)

    scanner = MarketScanner()
    combined = CompositeMarketDataProvider(StalePrimary(), SyntheticTestCoinProvider(scanner))
    with pytest.raises(MarketDataError, match="stale or disconnected"):
        combined.get_trade_quote("BTC")
    assert combined.get_trade_quote("TEST").provider == TEST_PROVIDER_NAME


def test_coinbase_can_never_fetch_reserved_test_symbol():
    with pytest.raises(ValueError, match="reserved for synthetic"):
        CoinbaseProvider(symbols=("BTC", "TEST"))


def test_autopilot_naturally_buys_and_exits_test_with_provenance_and_cooldown(tmp_path):
    scanner = MarketScanner()
    provider = SyntheticTestCoinProvider(scanner)
    for _ in range(4):
        provider.get_quotes()
    database = tmp_path / "test-coin.db"
    broker = PaperBroker(database)
    store = DecisionStore(database)
    runner = StrategyRunner(BaselineMomentumStrategy(), provider, scanner, broker, store)
    runner.enable()
    started = datetime.now(timezone.utc)

    buy = runner.evaluate_once(now=started)
    assert (buy.action, buy.symbol) == ("BUY", "TEST")
    assert broker.portfolio().symbol == "TEST"
    assert broker.trades()[0].provider_name == TEST_PROVIDER_NAME
    assert broker.trades()[0].quote_timestamp is not None

    sell = None
    for index in range(1, 10):
        decision = runner.evaluate_once(now=started + timedelta(seconds=index * 3))
        if decision.action == "SELL":
            sell = decision
            break
    assert sell is not None
    assert sell.reason_code == "TAKE_PROFIT"
    assert [trade.side for trade in broker.trades()] == ["SELL", "BUY"]
    assert all(trade.provider_name == TEST_PROVIDER_NAME for trade in broker.trades())
    assert runner.state == "Cooldown"

    cooldown = runner.evaluate_once(now=started + timedelta(seconds=(index + 1) * 3))
    assert cooldown.reason_code == "COOLDOWN"
    assert len(broker.trades()) == 2
