(() => {
    if (document.body.dataset.liveProvider !== "true") return;

    const money = (value) => {
        const number = Number(value);
        return `$${number.toFixed(number >= 1 ? 4 : 6)}`;
    };
    const percent = (value, signed = false) => {
        const number = Number(value);
        return `${signed && number >= 0 ? "+" : ""}${number.toFixed(3)}%`;
    };

    async function refreshMarket() {
        try {
            const response = await fetch("/api/market", { cache: "no-store" });
            if (!response.ok) return;
            const data = await response.json();
            const statusPanel = document.querySelector("#provider-status");
            statusPanel.classList.toggle("connected", data.provider.connected);
            statusPanel.classList.toggle("disconnected", !data.provider.connected);
            document.querySelector("#connection-state").textContent = data.provider.connected
                ? "Connected • receiving public ticker data"
                : `Disconnected / reconnecting (attempt ${data.provider.reconnect_attempts})`;
            document.querySelector("#provider-error").textContent = data.provider.error || "";

            if (document.querySelector("#waiting-row") && data.quotes.length) {
                window.location.reload();
                return;
            }
            const quotes = new Map(data.quotes.map((quote) => [quote.symbol, quote]));
            for (const [symbol, quote] of quotes) {
                const row = document.querySelector(`tr[data-symbol="${symbol}"]`);
                if (!row) continue;
                row.querySelector('[data-field="bid"]').textContent = money(quote.bid);
                row.querySelector('[data-field="ask"]').textContent = money(quote.ask);
                row.querySelector('[data-field="spread"]').textContent = percent(quote.spread_pct);
                row.querySelector('[data-field="return"]').textContent = percent(quote.short_return_pct, true);
                row.querySelector('[data-field="volatility"]').textContent = percent(quote.volatility_pct);
                const quoteStatus = row.querySelector('[data-field="status"]');
                quoteStatus.textContent = `${Number(quote.age_seconds).toFixed(1)}s • ${quote.stale ? "STALE" : "Fresh"}`;
                quoteStatus.classList.toggle("stale", quote.stale);
                quoteStatus.classList.toggle("fresh", !quote.stale);
                const buyButton = row.querySelector("[data-trade-button]");
                buyButton.disabled = quote.stale || buyButton.dataset.accountDisabled === "true";
            }
            const sellButton = document.querySelector("[data-held-symbol]");
            if (sellButton) {
                const heldQuote = quotes.get(sellButton.dataset.heldSymbol);
                sellButton.disabled = !heldQuote || heldQuote.stale;
            }
        } catch (_) {
            // The server-rendered disconnected state remains authoritative.
        }
    }

    refreshMarket();
    window.setInterval(refreshMarket, 2000);
})();
