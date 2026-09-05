from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from tradebot.app import app
from tradebot.assets import FOREX, ProviderSymbolMarketData, asset_metadata
from tradebot.models import Candle, MarketKind, MarketSnapshot
from tradebot.overview import MarketOverviewService


client = TestClient(app)


class RecordingProvider:
    def __init__(self, failure=None):
        self.symbols = []
        self.failure = failure

    def snapshot(self, symbol):
        self.symbols.append(symbol)
        if self.failure:
            raise self.failure
        return MarketSnapshot(symbol, 0.65234, "2026-09-05T12:00:00Z", "Yahoo Finance", .12, None)

    def candles(self, symbol, timeframe, limit):
        self.symbols.append(symbol)
        return [Candle(index, .65, .66, .64, .65234, None) for index in range(30)]


class Registry:
    def __init__(self, provider):
        self.provider = provider

    def market_data(self, selection):
        return ProviderSymbolMarketData(self.provider, selection.market)


def analyze(provider):
    with patch("tradebot.app.default_registry", return_value=Registry(provider)):
        return client.post("/api/analyze/quick", json={
            "market": "forex", "venue": "openbb", "symbol": "AUDUSD", "equity": 100000,
        })


def test_forex_metadata_maps_every_public_pair_to_yahoo_symbol():
    expected = {pair: f"{pair}=X" for pair in (
        "EURUSD", "GBPUSD", "AUDUSD", "NZDUSD", "USDJPY", "USDCHF", "USDCAD"
    )}
    assert {symbol: metadata.provider_symbol for symbol, metadata in FOREX.items()} == expected
    assert asset_metadata(MarketKind.FOREX, "audusd").display_name == "AUD/USD forex pair"


def test_forex_asset_analysis_uses_provider_symbol_and_keeps_public_metadata():
    provider = RecordingProvider()
    response = analyze(provider)
    assert response.status_code == 200
    body = response.json()
    assert set(provider.symbols) == {"AUDUSD=X"}
    assert body["symbol"] == "AUDUSD"
    assert body["provider_symbol"] == "AUDUSD=X"
    assert body["display_name"] == "AUD/USD forex pair"
    assert body["volume"] is None
    assert body["chart_timeframes"]["1h"]


def test_forex_asset_failure_is_redacted_and_market_specific(monkeypatch):
    monkeypatch.delenv("SIGNAL_DEBUG", raising=False)
    response = analyze(RecordingProvider(RuntimeError("secret provider failure")))
    assert response.status_code == 502
    assert response.json()["detail"] == "Forex data is temporarily unavailable. Try again later."
    assert "secret provider failure" not in response.text


def test_forex_overview_uses_the_same_metadata_mapping():
    providers = []

    def fake_providers(self, market, symbol):
        provider = RecordingProvider()
        providers.append((symbol, provider))
        metadata = asset_metadata(market, symbol)
        return [(metadata.provider_symbol, provider)]

    with patch.object(MarketOverviewService, "_providers", fake_providers):
        overview = MarketOverviewService().build(MarketKind.FOREX)
    assert {provider.symbols[0] for _, provider in providers} == {
        metadata.provider_symbol for metadata in FOREX.values()
    }
    assert all(row["provider_symbol"].endswith("=X") for row in overview["assets"])
    assert all("/" in row["display_name"] for row in overview["assets"])


def test_frontend_formats_forex_prices_volume_and_market_aware_surfaces():
    javascript = Path("src/tradebot/web/app.js").read_text()
    assert "symbol.toUpperCase().includes('JPY')?2:4" in javascript
    assert "symbol.toUpperCase().includes('JPY')?3:5" in javascript
    assert "market==='forex'&&Number(value)===0" in javascript
    assert "Volume not reported" in javascript
    assert "priceFormat:chartPriceFormat()" in javascript
    assert "${price(level.price)}" in javascript
    assert "${price(plan.stop_loss,market,symbol)}" in javascript
    assert "if(market==='forex')return side==='LONG'?'Buy base currency paper trade':'Sell base currency paper trade'" in javascript
    assert "Internal side: ${safe(x.side)}" in javascript
    assert "errorView(error,{market,asset:true,retryPath:" in javascript
