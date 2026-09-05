from unittest.mock import patch

from fastapi.testclient import TestClient

from tradebot.app import app
from tradebot.assets import ProviderSymbolMarketData, asset_metadata
from tradebot.models import MarketKind, MarketSnapshot

client = TestClient(app)


class RecordingProvider:
    def __init__(self, failure=None):
        self.symbols = []
        self.failure = failure

    def snapshot(self, symbol):
        self.symbols.append(symbol)
        if self.failure:
            raise self.failure
        return MarketSnapshot(symbol, 2453.66, "2026-09-04T12:00:00Z", "Yahoo Finance", -.02, 332_610_000)

    def candles(self, symbol, timeframe, limit):
        self.symbols.append(symbol)
        return []


class Registry:
    def __init__(self, provider):
        self.provider = provider

    def market_data(self, selection):
        return ProviderSymbolMarketData(self.provider, selection.market)


def analyze(symbol, provider):
    with patch("tradebot.app.default_registry", return_value=Registry(provider)):
        return client.post("/api/analyze/quick", json={
            "market": "commodities", "venue": "openbb", "symbol": symbol, "equity": 100000
        })


def test_gc_and_zw_routes_use_yahoo_futures_symbols_and_public_metadata():
    for symbol, provider_symbol, name in (("GC", "GC=F", "Gold"), ("ZW", "ZW=F", "Wheat")):
        provider = RecordingProvider()
        response = analyze(symbol, provider)
        assert response.status_code == 200
        body = response.json()
        assert provider.symbols and set(provider.symbols) == {provider_symbol}
        assert body["symbol"] == symbol
        assert body["provider_symbol"] == provider_symbol
        assert body["display_name"] == name
        assert body["live_price"] == 2453.66
        assert body["change_24h"] == -.02
        assert body["volume"] == 332_610_000
        assert body["source"] == "Yahoo Finance"
        assert body["last_updated"]


def test_canonical_mapping_and_descriptions():
    assert asset_metadata(MarketKind.COMMODITIES, "GC").provider_symbol == "GC=F"
    assert asset_metadata(MarketKind.COMMODITIES, "ZW").provider_symbol == "ZW=F"
    assert asset_metadata(MarketKind.COMMODITIES, "GC").description == "Gold futures track gold market prices."


def test_provider_errors_are_beginner_friendly_unless_debug_enabled(monkeypatch):
    monkeypatch.delenv("SIGNAL_DEBUG", raising=False)
    response = analyze("GC", RecordingProvider(RuntimeError("YahooFinanceClient exploded")))
    assert response.status_code == 502
    assert response.json()["detail"] == "Live data for Gold is temporarily unavailable. Try again later or choose another commodity."
    assert "RuntimeError" not in response.text
    monkeypatch.setenv("SIGNAL_DEBUG", "1")
    response = analyze("GC", RecordingProvider(RuntimeError("provider detail")))
    assert "RuntimeError: provider detail" in response.json()["detail"]
