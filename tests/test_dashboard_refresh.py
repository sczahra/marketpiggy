import asyncio
import importlib
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


def load_app(monkeypatch, tmp_path, *, test_coin="1"):
    monkeypatch.setenv("MARKETPIGGY_DB", str(tmp_path / "dashboard.db"))
    monkeypatch.setenv("MARKETPIGGY_MARKET_PROVIDER", "static")
    monkeypatch.setenv("MARKETPIGGY_STATIC_MOVE_PCT", "0.10")
    monkeypatch.setenv("MARKETPIGGY_TEST_COIN", test_coin)
    monkeypatch.setenv("MARKETPIGGY_OBSERVATION_SECONDS", "30")
    sys.modules.pop("app.main", None)
    return importlib.import_module("app.main")


def test_dashboard_read_api_returns_current_portfolio_and_held_scanner_state(
    monkeypatch, tmp_path
):
    main = load_app(monkeypatch, tmp_path)
    quote = main.market_data.get_trade_quote("TEST")
    main.broker.buy(
        "TEST", "2.50", bid=quote.bid, ask=quote.ask,
        provider_name=quote.provider, quote_timestamp=quote.timestamp,
        received_at=quote.received_at,
    )

    payload = asyncio.run(main.dashboard_state())
    assert payload["portfolio"]["symbol"] == "TEST"
    assert payload["portfolio"]["cash"].startswith("7.5")
    assert payload["open_position"]["symbol"] == "TEST"
    assert payload["open_position"]["close_available"] is True
    test_row = next(row for row in payload["quotes"] if row["symbol"] == "TEST")
    assert test_row["held"] is True
    assert test_row["eligibility"]["light"] in {"red", "yellow", "green"}


def test_trade_payload_is_newest_first_unique_and_keeps_provenance(monkeypatch, tmp_path):
    main = load_app(monkeypatch, tmp_path)
    buy_quote = main.market_data.get_trade_quote("TEST")
    bought = main.broker.buy(
        "TEST", "2.50", bid=buy_quote.bid, ask=buy_quote.ask,
        provider_name=buy_quote.provider, quote_timestamp=buy_quote.timestamp,
        received_at=buy_quote.received_at,
    )
    sell_quote = main.market_data.get_trade_quote("TEST")
    main.broker.sell(
        "TEST", bought.quantity, bid=sell_quote.bid, ask=sell_quote.ask,
        provider_name=sell_quote.provider, quote_timestamp=sell_quote.timestamp,
        received_at=sell_quote.received_at,
    )

    trades = asyncio.run(main.dashboard_state())["trades"]
    assert [trade["side"] for trade in trades] == ["SELL", "BUY"]
    assert len({trade["id"] for trade in trades}) == len(trades)
    assert all(trade["context"] == "Simulation 1 • Synthetic TEST data" for trade in trades)
    assert all(trade["provider_name"] == "Synthetic TEST data" for trade in trades)
    assert all(trade["session_id"] for trade in trades)
    assert all(trade["quote_timestamp"] and trade["received_at"] for trade in trades)


def test_strategy_status_supplies_live_evaluation_timestamp(monkeypatch, tmp_path):
    main = load_app(monkeypatch, tmp_path)
    for _ in range(4):
        main.market_data.get_quotes()
    evaluated_at = datetime.now(timezone.utc)
    main.strategy_runner.enable()
    main.strategy_runner.evaluate_once(now=evaluated_at)

    strategy = asyncio.run(main.dashboard_state())["strategy"]
    assert strategy["enabled"] is True
    assert strategy["last_evaluated_at"] == evaluated_at.isoformat()
    assert strategy["last_action"] in {"BUY", "HOLD"}


def test_repeated_dashboard_reads_never_place_a_trade(monkeypatch, tmp_path):
    main = load_app(monkeypatch, tmp_path)
    assert main.strategy_runner.enabled is False
    first = asyncio.run(main.dashboard_state())
    second = asyncio.run(main.dashboard_state())
    assert first["trades"] == []
    assert second["trades"] == []
    assert main.broker.trades() == []
    route = next(route for route in main.app.routes if route.path == "/api/dashboard")
    assert route.methods == {"GET"}


def test_client_uses_one_non_overlapping_two_second_loop_and_replaces_trade_rows():
    script = (Path(__file__).parents[1] / "app" / "static" / "app.js").read_text()
    styles = (Path(__file__).parents[1] / "app" / "static" / "app.css").read_text()
    assert "const POLL_INTERVAL_MS = 2000" in script
    assert script.count("window.setInterval(") == 1
    assert "if (refreshInProgress) return" in script
    assert 'fetch("/api/dashboard"' in script
    assert 'querySelector("#trade-rows").replaceChildren(...rows)' in script
    assert "knownTradeIds" in script
    assert 'row.classList.add("new-trade")' in script
    assert '"BOUGHT"' in script and '"SOLD"' in script
    assert ".new-trade" in styles and "trade-highlight" in styles
    assert 'fetch("/trade/' not in script
    assert "window.location.reload" not in script


def test_test_mode_snapshot_tracks_buy_exit_and_cooldown(monkeypatch, tmp_path):
    main = load_app(monkeypatch, tmp_path)
    for _ in range(4):
        main.market_data.get_quotes()
    main.strategy_runner.enable()
    started = datetime.now(timezone.utc)
    main.strategy_runner.evaluate_once(now=started)
    holding = asyncio.run(main.dashboard_state())
    assert holding["portfolio"]["symbol"] == "TEST"
    assert next(row for row in holding["quotes"] if row["symbol"] == "TEST")["held"]

    for index in range(1, 10):
        decision = main.strategy_runner.evaluate_once(
            now=started + timedelta(seconds=index * 3)
        )
        if decision.action == "SELL":
            break
    exited = asyncio.run(main.dashboard_state())
    assert decision.reason_code == "TAKE_PROFIT"
    assert exited["portfolio"]["symbol"] is None
    assert [trade["side"] for trade in exited["trades"][:2]] == ["SELL", "BUY"]
    cooldown = main.strategy_runner.evaluate_once(
        now=started + timedelta(seconds=(index + 1) * 3)
    )
    assert cooldown.reason_code == "COOLDOWN"
