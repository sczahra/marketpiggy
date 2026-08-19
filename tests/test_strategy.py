import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.broker.paper import PaperBroker, Portfolio
from app.market_data.base import MarketDataError, MarketDataProvider, MarketQuote, ProviderStatus
from app.market_data.scanner import MarketScanner, ScannerRow
from app.strategy import (
    BaselineMomentumStrategy,
    Decision,
    DecisionStore,
    StrategyConfig,
    StrategyInput,
    StrategyRunner,
)


NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)


def quote(symbol="BTC", midpoint="100", *, spread_pct="0.10", stale=False):
    midpoint = Decimal(midpoint)
    half = midpoint * Decimal(spread_pct) / Decimal("200")
    return MarketQuote(symbol, midpoint - half, midpoint + half, NOW, NOW,
                       "Deterministic fixture", stale=stale)


def portfolio(*, cash="10", symbol=None, quantity="0", entry="0"):
    return Portfolio(Decimal("10"), Decimal(cash), symbol, Decimal(quantity),
                     Decimal(entry), Decimal("0"), Decimal("0"), Decimal("0"),
                     Decimal(cash), Decimal("0"))


def snapshot(rows, *, held=None, counts=None, opened=None, cooldown=None):
    return StrategyInput(NOW, tuple(rows), counts or {row.quote.symbol: 5 for row in rows},
                         held or portfolio(), opened, cooldown)


def row(symbol, momentum, volatility="0.05", midpoint="100", spread="0.10", stale=False):
    return ScannerRow(quote(symbol, midpoint, spread_pct=spread, stale=stale),
                      Decimal(momentum), Decimal(volatility))


class FixtureProvider(MarketDataProvider):
    stale_after_seconds = 15.0

    def __init__(self, quotes):
        self.quotes = {item.symbol: item for item in quotes}

    def get_quotes(self):
        return list(self.quotes.values())

    def get_quote(self, symbol):
        return self.quotes[symbol]

    def status(self):
        return ProviderStatus("Deterministic fixture", False, True, NOW)


def test_entry_selects_strongest_eligible_asset_deterministically():
    strategy = BaselineMomentumStrategy()
    decision = strategy.decide(snapshot([row("ETH", "0.30"), row("BTC", "0.30"), row("SOL", "0.25")]))
    assert (decision.action, decision.symbol, decision.reason_code) == (
        "BUY", "BTC", "STRONGEST_MOMENTUM"
    )


def test_entry_requires_history_momentum_spread_and_volatility():
    strategy = BaselineMomentumStrategy()
    rows = [row("BTC", "0.19"), row("ETH", "0.40", spread="0.21"),
            row("SOL", "0.40", volatility="0.36"), row("DOGE", "0.40")]
    decision = strategy.decide(snapshot(rows, counts={"BTC": 5, "ETH": 5, "SOL": 5, "DOGE": 4}))
    assert (decision.action, decision.reason_code) == ("HOLD", "NO_ENTRY")


def test_stale_data_blocks_entry_and_held_exit():
    strategy = BaselineMomentumStrategy()
    assert strategy.decide(snapshot([row("BTC", "1", stale=True)])).reason_code == "NO_FRESH_QUOTES"
    held = portfolio(cash="0", symbol="BTC", quantity="0.1", entry="100")
    decision = strategy.decide(snapshot([row("BTC", "-2", midpoint="98", stale=True)], held=held))
    assert (decision.action, decision.reason_code) == ("SKIP", "STALE_HELD_QUOTE")


def test_stop_loss_and_take_profit_exits_use_bid_return():
    strategy = BaselineMomentumStrategy()
    held = portfolio(cash="0", symbol="BTC", quantity="0.1", entry="100")
    stop = strategy.decide(snapshot([row("BTC", "0", midpoint="98.9")], held=held))
    profit = strategy.decide(snapshot([row("BTC", "0", midpoint="101.7")], held=held))
    assert (stop.action, stop.reason_code) == ("SELL", "STOP_LOSS")
    assert (profit.action, profit.reason_code) == ("SELL", "TAKE_PROFIT")


def test_max_hold_is_exact_and_momentum_reversal_exits():
    strategy = BaselineMomentumStrategy()
    held = portfolio(cash="0", symbol="BTC", quantity="0.1", entry="100")
    before = strategy.decide(snapshot([row("BTC", "0", midpoint="100.1")], held=held,
                                      opened=NOW - timedelta(seconds=899)))
    exact = strategy.decide(snapshot([row("BTC", "0", midpoint="100.1")], held=held,
                                     opened=NOW - timedelta(seconds=900)))
    reverse = strategy.decide(snapshot([row("BTC", "-0.10", midpoint="100.1")], held=held,
                                       opened=NOW))
    assert before.action == "HOLD"
    assert exact.reason_code == "MAX_HOLD"
    assert reverse.reason_code == "MOMENTUM_REVERSAL"


def test_cooldown_blocks_reentry_until_boundary():
    strategy = BaselineMomentumStrategy()
    rows = [row("BTC", "0.5")]
    assert strategy.decide(snapshot(rows, cooldown=NOW + timedelta(seconds=1))).reason_code == "COOLDOWN"
    assert strategy.decide(snapshot(rows, cooldown=NOW)).action == "BUY"


def make_runner(tmp_path):
    scanner = MarketScanner()
    samples = ["100", "100.1", "100.2", "100.3", "100.4"]
    for index, value in enumerate(samples):
        item = quote(midpoint=value)
        scanner.record(MarketQuote(item.symbol, item.bid, item.ask,
                                   NOW + timedelta(seconds=index),
                                   NOW + timedelta(seconds=index), item.provider))
    current = quote(midpoint="100.4")
    provider = FixtureProvider([current])
    broker = PaperBroker(tmp_path / "runner.db", friction_rate="0")
    store = DecisionStore(tmp_path / "runner.db")
    return StrategyRunner(BaselineMomentumStrategy(), provider, scanner, broker, store), broker, store


def test_runner_is_off_by_default_and_does_not_trade(tmp_path):
    runner, broker, store = make_runner(tmp_path)
    decision = runner.evaluate_once(now=NOW + timedelta(seconds=5))
    assert decision.reason_code == "AUTOPILOT_OFF"
    assert broker.trades() == []
    assert store.recent(broker.active_session().id) == []


def test_runner_executes_one_sized_paper_buy_with_provenance_and_no_duplicate(tmp_path):
    runner, broker, store = make_runner(tmp_path)
    runner.enable()
    first = runner.evaluate_once(now=NOW + timedelta(seconds=5))
    second = runner.evaluate_once(now=NOW + timedelta(seconds=8))
    trades = broker.trades()
    assert first.action == "BUY"
    assert second.action == "HOLD"
    assert len(trades) == 1
    assert trades[0].provider_name == "Deterministic fixture"
    assert abs(trades[0].resulting_cash - Decimal("7.5")) < Decimal("0.000000000001")
    records = store.recent(broker.active_session().id)
    assert records[-1].executed is True
    assert records[-1].trade_id == trades[0].id
    assert records[-1].session_id == broker.active_session().id


def test_runner_refetches_and_blocks_stale_execution(tmp_path):
    runner, broker, store = make_runner(tmp_path)
    def stale_at_execution(symbol):
        raise MarketDataError(f"{symbol} quote became stale")
    runner.provider.get_trade_quote = stale_at_execution
    runner.enable()
    decision = runner.evaluate_once(now=NOW + timedelta(seconds=5))
    assert decision.reason_code == "EXECUTION_BLOCKED"
    assert broker.trades() == []
    assert store.recent(broker.active_session().id)[0].executed is False


def test_runner_executes_full_paper_exit_then_enters_cooldown(tmp_path):
    runner, broker, store = make_runner(tmp_path)
    runner.enable()
    runner.evaluate_once(now=NOW + timedelta(seconds=5))
    high = quote(midpoint="102.5")
    runner.provider.quotes["BTC"] = high
    runner.scanner.record(high)
    decision = runner.evaluate_once(now=NOW + timedelta(seconds=8))
    trades = broker.trades()
    assert (decision.action, decision.reason_code) == ("SELL", "TAKE_PROFIT")
    assert [trade.side for trade in trades] == ["SELL", "BUY"]
    assert broker.portfolio().symbol is None
    assert runner.state == "Cooldown"
    assert store.recent(broker.active_session().id)[0].trade_id == trades[0].id
    cooldown = runner.evaluate_once(now=NOW + timedelta(seconds=9))
    assert cooldown.reason_code == "COOLDOWN"
    assert len(broker.trades()) == 2


def test_disabling_runner_never_liquidates_open_position(tmp_path):
    runner, broker, _ = make_runner(tmp_path)
    runner.enable()
    runner.evaluate_once(now=NOW + timedelta(seconds=5))
    runner.disable()
    decision = runner.evaluate_once(now=NOW + timedelta(seconds=8))
    assert decision.reason_code == "AUTOPILOT_OFF"
    assert broker.portfolio().symbol == "BTC"
    assert len(broker.trades()) == 1


def test_decision_log_deduplicates_and_is_session_scoped(tmp_path):
    path = tmp_path / "decisions.db"
    broker = PaperBroker(path)
    store = DecisionStore(path)
    session = broker.active_session().id
    decision = Decision("HOLD", None, "NO_ENTRY", "No entry", {"x": "1"})
    assert store.record(session, "Baseline", "1", decision, now=NOW) is not None
    changed_signals = Decision("HOLD", None, "NO_ENTRY", "No entry", {"x": "2"})
    assert store.record(session, "Baseline", "1", changed_signals,
                        now=NOW + timedelta(seconds=29)) is None
    assert store.record(session, "Baseline", "1", changed_signals,
                        now=NOW + timedelta(seconds=30)) is not None
    broker.start_new_simulation("20", preserve_history=True)
    new_session = broker.active_session().id
    store.record(new_session, "Baseline", "1", decision, now=NOW)
    assert len(store.recent(session)) == 2
    assert len(DecisionStore(path).recent(new_session)) == 1


def test_runner_background_lifecycle_starts_inactive_and_stops_cleanly(tmp_path):
    async def exercise():
        runner, broker, _ = make_runner(tmp_path)
        await runner.start()
        await asyncio.sleep(0)
        assert runner.enabled is False
        assert broker.trades() == []
        await runner.stop()
        assert runner._task is None
        assert runner.state == "Off"
    asyncio.run(exercise())


def test_invalid_strategy_configuration_fails_at_startup_boundary():
    for kwargs in ({"position_size_fraction": "1.1"},
                   {"entry_momentum_pct": "0"},
                   {"evaluation_interval_seconds": 0.5},
                   {"evaluation_interval_seconds": float("nan")},
                   {"minimum_observations": 1}):
        try:
            StrategyConfig(**kwargs)
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid config accepted: {kwargs}")
