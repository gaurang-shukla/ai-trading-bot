from unittest.mock import patch

from tradebot.models import MarketKind
from tradebot.models import MarketSnapshot
from tradebot.overview import MarketOverviewService, crypto_overview


def test_crypto_overview_ranks_real_ticker_fields():
    tickers = [
        {"symbol": "AAAUSDT", "lastPrice": "10", "priceChangePercent": "8",
         "highPrice": "11", "lowPrice": "9", "quoteVolume": "1000000"},
        {"symbol": "BBBUSDT", "lastPrice": "20", "priceChangePercent": "-4",
         "highPrice": "21", "lowPrice": "19", "quoteVolume": "500000"},
    ]
    with patch("tradebot.overview.WeexFuturesMarketData.tickers", return_value=tickers):
        result = crypto_overview(MarketKind.CRYPTO_FUTURES)
    assert result["gainers"][0]["symbol"] == "AAAUSDT"
    assert result["losers"][0]["symbol"] == "BBBUSDT"
    assert all(0 <= row["signal_score"] <= 100 for row in result["assets"])


def test_non_crypto_overview_is_fully_populated_from_openbb(monkeypatch):
    monkeypatch.setattr("tradebot.overview.SEED_UNIVERSES", {MarketKind.EQUITIES: ["AAPL", "MSFT"]})
    monkeypatch.setattr("tradebot.overview.OpenBBClient.snapshot", lambda self, symbol:
                        MarketSnapshot(symbol, 100, "now", "OpenBB", 2.5, 1_000))
    result = MarketOverviewService().build(MarketKind.EQUITIES)
    assert result["fear_greed"]["score"] is not None
    assert result["gainers"] and result["losers"]
    assert all(row["price"] and row["volume"] is not None and row["signal_score"] is not None
               for row in result["assets"])


def test_openbb_failure_falls_back_to_yahoo(monkeypatch, caplog):
    monkeypatch.setattr("tradebot.overview.SEED_UNIVERSES", {MarketKind.EQUITIES: ["AAPL"]})
    monkeypatch.setattr("tradebot.overview.OpenBBClient.snapshot", lambda self, symbol:
                        (_ for _ in ()).throw(ConnectionError("offline")))
    monkeypatch.setattr("tradebot.overview.YahooFinanceClient.snapshot", lambda self, symbol:
                        MarketSnapshot(symbol, 101, "now", "Yahoo Finance", 1.2, 500))
    result = MarketOverviewService().build(MarketKind.EQUITIES)
    assert result["assets"][0]["price"] == 101
    assert result["source"] == "Yahoo Finance"
    assert "OpenBBClient failed" in caplog.text


def test_all_provider_failures_return_stable_dashboard(monkeypatch):
    monkeypatch.setattr("tradebot.overview.SEED_UNIVERSES", {MarketKind.EQUITIES: ["AAPL"]})
    monkeypatch.setattr(MarketOverviewService, "_load_symbol", lambda *args:
                        (_ for _ in ()).throw(ConnectionError("offline")))
    result = MarketOverviewService().build(MarketKind.EQUITIES)
    assert result["warning"] == "Live market data is temporarily unavailable."
    assert result["assets"][0]["symbol"] == "AAPL"
