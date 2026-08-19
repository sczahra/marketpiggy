(() => {
    const POLL_INTERVAL_MS = 2000;
    const liveProvider = document.body.dataset.liveProvider === "true";
    const knownTradeIds = new Set(
        [...document.querySelectorAll("[data-trade-id]")].map((row) => row.dataset.tradeId),
    );
    let refreshInProgress = false;
    let activityTimer;

    const number = (value) => Number(value);
    const money = (value, digits = 2) => `$${number(value).toFixed(digits)}`;
    const quoteMoney = (value) => money(value, number(value) >= 1 ? 4 : 6);
    const signedMoney = (value) => `$${number(value) >= 0 ? "+" : ""}${number(value).toFixed(2)}`;
    const percent = (value, digits = 3, signed = false) => {
        const parsed = number(value);
        return `${signed && parsed >= 0 ? "+" : ""}${parsed.toFixed(digits)}%`;
    };

    function updateProvider(provider) {
        const panel = document.querySelector("#provider-status");
        panel.classList.toggle("connected", provider.connected);
        panel.classList.toggle("disconnected", !provider.connected);
        if (liveProvider) {
            document.querySelector("#connection-state").textContent = provider.connected
                ? "Connected • receiving public ticker data"
                : `Disconnected / reconnecting (attempt ${provider.reconnect_attempts})`;
        }
        document.querySelector("#provider-error").textContent = provider.error || "";
    }

    function updatePortfolio(portfolio) {
        document.querySelector("#portfolio-value").textContent = money(portfolio.total_value);
        document.querySelector("#cash-value").textContent = money(portfolio.cash);
        document.querySelector("#realized-pl").textContent = signedMoney(portfolio.realized_pl);
        document.querySelector("#unrealized-pl").textContent = signedMoney(portfolio.unrealized_pl);
        document.querySelector("#total-return").textContent = percent(portfolio.total_return_pct, 2, true);
        document.querySelector("#holding-symbol").textContent = portfolio.symbol || "None";
        const details = document.querySelector("#holding-details");
        details.hidden = !portfolio.symbol;
        if (portfolio.symbol) {
            document.querySelector("#holding-quantity").textContent = number(portfolio.quantity).toFixed(10);
            document.querySelector("#holding-average").textContent = number(portfolio.average_entry_price).toFixed(6);
        }
    }

    function updateOpenPosition(position) {
        const panel = document.querySelector("#open-position-panel");
        panel.hidden = !position;
        if (!position) return;
        document.querySelector("#open-position-title").textContent = `Open ${position.symbol} Position`;
        const mark = position.mark_bid ? ` • Bid ${money(position.mark_bid, 6)}` : " • Bid unavailable";
        document.querySelector("#open-position-mark").textContent =
            `${number(position.quantity).toFixed(10)} units • Avg. ${money(position.average_entry_price, 6)}${mark}. `;
        document.querySelector("#open-position-note").textContent = position.note;
        document.querySelector("#close-symbol").value = position.symbol;
        const quantity = document.querySelector("#close-quantity");
        quantity.max = position.quantity;
        if (document.activeElement !== quantity) quantity.value = position.quantity;
        const button = document.querySelector("#close-button");
        button.dataset.heldSymbol = position.symbol;
        button.disabled = !position.close_available;
    }

    function createMarketRow(quote, cash) {
        const row = document.createElement("tr");
        row.dataset.symbol = quote.symbol;
        const symbolCell = document.createElement("td");
        const light = document.createElement("span");
        light.dataset.field = "eligibility";
        light.className = "eligibility-light";
        symbolCell.append(light);
        const symbol = document.createElement("strong");
        symbol.textContent = quote.symbol;
        symbolCell.append(symbol);
        if (quote.synthetic_test) {
            const synthetic = document.createElement("span");
            synthetic.className = "test-data-badge";
            synthetic.textContent = "SYNTHETIC";
            symbolCell.append(synthetic);
        }
        const holding = document.createElement("span");
        holding.dataset.field = "holding";
        holding.className = "holding-badge";
        holding.textContent = "HOLDING";
        holding.hidden = true;
        symbolCell.append(holding);
        row.append(symbolCell);
        for (const field of ["bid", "ask", "spread", "return", "volatility", "status"]) {
            const cell = document.createElement("td");
            cell.dataset.field = field;
            row.append(cell);
        }
        const orderCell = document.createElement("td");
        const form = document.createElement("form");
        form.method = "post";
        form.action = "/trade/buy";
        form.className = "inline-form";
        const hiddenSymbol = document.createElement("input");
        hiddenSymbol.type = "hidden";
        hiddenSymbol.name = "symbol";
        hiddenSymbol.value = quote.symbol;
        const amount = document.createElement("input");
        amount.name = "dollars";
        amount.type = "number";
        amount.min = "0.01";
        amount.max = cash;
        amount.step = "0.01";
        amount.value = number(cash).toFixed(2);
        amount.required = true;
        amount.setAttribute("aria-label", `${quote.symbol} dollar amount`);
        const button = document.createElement("button");
        button.type = "submit";
        button.dataset.tradeButton = "";
        button.textContent = "Buy";
        form.append(hiddenSymbol, amount, button);
        orderCell.append(form);
        row.append(orderCell);
        document.querySelector("#market-rows").append(row);
        return row;
    }

    function updateMarket(quotes, portfolio) {
        if (quotes.length) document.querySelector("#waiting-row")?.remove();
        for (const quote of quotes) {
            const row = document.querySelector(`tr[data-symbol="${quote.symbol}"]`)
                || createMarketRow(quote, portfolio.cash);
            row.classList.toggle("held-row", quote.held);
            row.querySelector('[data-field="holding"]').hidden = !quote.held;
            const light = row.querySelector('[data-field="eligibility"]');
            light.className = `eligibility-light ${quote.eligibility.light}`;
            light.title = quote.eligibility.explanation;
            light.setAttribute("aria-label", quote.eligibility.explanation);
            row.querySelector('[data-field="bid"]').textContent = quoteMoney(quote.bid);
            row.querySelector('[data-field="ask"]').textContent = quoteMoney(quote.ask);
            row.querySelector('[data-field="spread"]').textContent = percent(quote.spread_pct);
            row.querySelector('[data-field="return"]').textContent = percent(quote.short_return_pct, 3, true);
            row.querySelector('[data-field="volatility"]').textContent = percent(quote.volatility_pct);
            const status = row.querySelector('[data-field="status"]');
            status.textContent = `${number(quote.age_seconds).toFixed(1)}s • ${quote.stale ? "STALE" : "Fresh"}`;
            status.classList.toggle("stale", quote.stale);
            status.classList.toggle("fresh", !quote.stale);
            const amount = row.querySelector('input[name="dollars"]');
            amount.max = portfolio.cash;
            if (document.activeElement !== amount && (!amount.value || number(amount.value) > number(portfolio.cash))) {
                amount.value = number(portfolio.cash).toFixed(2);
            }
            row.querySelector("[data-trade-button]").disabled = !quote.manual_buy_enabled;
        }
    }

    function evaluationRecency(strategy) {
        if (!strategy.last_evaluated_at) {
            return strategy.enabled ? "Waiting for first evaluation" : "Not evaluated this run";
        }
        const seconds = Math.max(0, Math.floor((Date.now() - Date.parse(strategy.last_evaluated_at)) / 1000));
        return seconds < 1 ? "Evaluated just now" : `Evaluated ${seconds}s ago`;
    }

    function updateStrategy(strategy) {
        const panel = document.querySelector("#strategy-status");
        panel.classList.toggle("enabled", strategy.enabled);
        panel.classList.toggle("disabled", !strategy.enabled);
        document.querySelector("#autopilot-badge").textContent = `AUTOPILOT ${strategy.enabled ? "ON" : "OFF"}`;
        document.querySelector("#strategy-state").textContent = `• ${strategy.state}`;
        document.querySelector("#strategy-decision").textContent =
            `• Last: ${strategy.last_action || "—"}${strategy.last_symbol ? ` ${strategy.last_symbol}` : ""}`;
        document.querySelector("#strategy-recency").textContent = `• ${evaluationRecency(strategy)}`;
        document.querySelector("#strategy-reason").textContent = strategy.last_reason;
    }

    function tradeRow(trade, highlight) {
        const row = document.createElement("tr");
        row.dataset.tradeId = String(trade.id);
        if (highlight) row.classList.add("new-trade");
        const values = [
            trade.timestamp.slice(0, 19).replace("T", " "), trade.context,
            trade.side, trade.symbol, number(trade.quantity).toFixed(10),
            money(trade.execution_price, 6),
            `${money(trade.raw_bid, 6)} / ${money(trade.raw_ask, 6)}`,
            money(trade.resulting_cash),
        ];
        values.forEach((value, index) => {
            const cell = document.createElement("td");
            if (index === 1) {
                const context = document.createElement("span");
                context.className = "provenance-label";
                context.textContent = value;
                cell.append(context);
            } else if (index === 2) {
                const side = document.createElement("strong");
                side.className = trade.side.toLowerCase();
                side.textContent = value;
                cell.append(side);
            } else {
                cell.textContent = value;
            }
            row.append(cell);
        });
        return row;
    }

    function showTradeActivity(trade) {
        const activity = document.querySelector("#trade-activity");
        activity.textContent = `${trade.side === "BUY" ? "BOUGHT" : "SOLD"} ${trade.symbol}`;
        activity.hidden = false;
        activity.classList.remove("pulse");
        void activity.offsetWidth;
        activity.classList.add("pulse");
        window.clearTimeout(activityTimer);
        activityTimer = window.setTimeout(() => { activity.hidden = true; }, 2600);
    }

    function updateTrades(trades) {
        const newIds = new Set(
            trades.filter((trade) => !knownTradeIds.has(String(trade.id)))
                .map((trade) => String(trade.id)),
        );
        const rows = trades.map((trade) => tradeRow(trade, newIds.has(String(trade.id))));
        document.querySelector("#trade-rows").replaceChildren(...rows);
        document.querySelector("#trade-table").hidden = trades.length === 0;
        document.querySelector("#trade-empty").hidden = trades.length !== 0;
        trades.forEach((trade) => knownTradeIds.add(String(trade.id)));
        const newest = trades.find((trade) => newIds.has(String(trade.id)));
        if (newest) showTradeActivity(newest);
    }

    async function refreshDashboard() {
        if (refreshInProgress) return;
        refreshInProgress = true;
        try {
            const response = await fetch("/api/dashboard", { cache: "no-store" });
            if (!response.ok) return;
            const data = await response.json();
            updateProvider(data.provider);
            updatePortfolio(data.portfolio);
            updateOpenPosition(data.open_position);
            updateMarket(data.quotes, data.portfolio);
            updateStrategy(data.strategy);
            updateTrades(data.trades);
        } catch (_) {
            // Preserve the last good dashboard and retry on the next interval.
        } finally {
            refreshInProgress = false;
        }
    }

    refreshDashboard();
    window.setInterval(refreshDashboard, POLL_INTERVAL_MS);
})();
