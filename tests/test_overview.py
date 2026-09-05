from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from tradebot.adapters import (WeexFuturesMarketData, WeexSpotMarketData,
                               normalize_weex_24h_change)
from tradebot.app import create_app
from tradebot.models import MarketKind, MarketSnapshot
from tradebot.overview import CachedMarketOverview, MarketOverviewService, crypto_overview


def test_weex_ratio_change_fields_are_normalized_to_percentage_points():
    assert normalize_weex_24h_change({"symbol": "BULLAUSDT", "changeRate": 2.104292}) == 210.4292
    assert normalize_weex_24h_change({"change": 0.0211}) == 2.11
    assert normalize_weex_24h_change({"riseFallRate": -0.006}) == -0.6
    assert normalize_weex_24h_change({"change_rate": 0.0211}) == 2.11
    assert normalize_weex_24h_change({"change": 2.111519}) == pytest.approx(211.1519)
    assert normalize_weex_24h_change({"change": 0.0717}) == pytest.approx(7.17)
    assert normalize_weex_24h_change({"change": 0.0003}) == pytest.approx(0.03)


def test_weex_percentage_point_changes_are_not_multiplied_again():
    assert normalize_weex_24h_change({"priceChangePercent": 211.41}) == 211.41
    assert normalize_weex_24h_change({"changePercent": 211.41}) == 211.41
    assert normalize_weex_24h_change({"changeRate": "211.41%"}) == 211.41
    assert normalize_weex_24h_change({"priceChangePercent": 7.17}) == 7.17


def test_crypto_overview_normalizes_bulla_change_rate():
    tickers = [{"symbol": "BULLAUSDT", "lastPrice": "0.086498", "changeRate": 2.104292}]

    with patch("tradebot.overview.WeexFuturesMarketData.tickers", return_value=tickers):
        result = crypto_overview(MarketKind.CRYPTO_FUTURES)

    assert result["assets"][0]["change"] == 210.4292
    assert result["gainers"][0]["symbol"] == "BULLAUSDT"
    assert result["gainers"][0]["change"] == 210.4292


@pytest.mark.parametrize(("market", "client_class", "rows"), [
    (MarketKind.CRYPTO_FUTURES, WeexFuturesMarketData, [
        {"symbol": "BULLAUSDT", "lastPrice": "0.087191", "change": 2.111519},
        {"symbol": "BNBUSDT", "lastPrice": "900", "change": 0.0717},
        {"symbol": "BTCUSDT", "lastPrice": "110000", "change": 0.0003},
        {"symbol": "DOWNUSDT", "lastPrice": "10", "change": -0.006},
    ]),
    (MarketKind.CRYPTO_SPOT, WeexSpotMarketData, [
        {"symbol": "BNBUSDT", "lastPrice": "900", "changeRate": 0.0717},
    ]),
])
def test_bulk_provider_to_overview_path_normalizes_once(monkeypatch, market, client_class, rows):
    monkeypatch.setattr(client_class, "_get", lambda self, path, params: {"data": rows})
    result = crypto_overview(market)
    by_symbol = {row["symbol"]: row for row in result["assets"]}
    assert by_symbol[rows[0]["symbol"]]["change"] == pytest.approx(
        float(rows[0].get("change", rows[0].get("changeRate"))) * 100)
    if market is MarketKind.CRYPTO_FUTURES:
        assert by_symbol["BULLAUSDT"]["change"] == pytest.approx(211.1519)
        assert by_symbol["BNBUSDT"]["change"] == pytest.approx(7.17)
        assert by_symbol["BTCUSDT"]["change"] == pytest.approx(0.03)
        assert by_symbol["DOWNUSDT"]["change"] == pytest.approx(-0.6)
        assert result["gainers"][0]["symbol"] == "BULLAUSDT"


def test_overview_api_uses_normalized_provider_path_and_debug_is_opt_in(monkeypatch):
    rows = [{"symbol": "BULLAUSDT", "lastPrice": "0.087191", "change": 2.111519}]
    monkeypatch.setattr(WeexFuturesMarketData, "_get", lambda self, path, params: rows)
    cache = CachedMarketOverview(ttl_seconds=0)
    cache.refresh_cooldown_seconds = 0
    monkeypatch.setattr("tradebot.overview._overviews", cache)
    client = TestClient(create_app())
    clean = client.get("/api/overview/crypto_futures?refresh=true").json()["assets"][0]
    assert clean["change"] == pytest.approx(211.1519)
    assert "raw_change_value" not in clean

    monkeypatch.setenv("SIGNAL_DEBUG", "true")
    debug = client.get("/api/overview/crypto_futures?refresh=true").json()["assets"][0]
    assert debug["provider_market"] == "crypto_futures"
    assert debug["raw_change_field"] == "change"
    assert debug["raw_change_value"] == 2.111519
    assert debug["normalized_change_percent"] == pytest.approx(211.1519)


@pytest.mark.parametrize(("market", "client_class", "symbol", "raw", "expected"), [
    ("crypto_futures", WeexFuturesMarketData, "BULLAUSDT", 2.067231, 206.7231),
    ("crypto_futures", WeexFuturesMarketData, "BNBUSDT", 0.0717, 7.17),
    ("crypto_spot", WeexSpotMarketData, "熊猫头USDT", 2.354281, 235.4281),
])
def test_each_crypto_overview_api_normalizes_live_change_field(
        monkeypatch, market, client_class, symbol, raw, expected):
    monkeypatch.setattr(client_class, "_get", lambda self, path, params: [
        {"symbol": symbol, "lastPrice": "1.25", "change": raw}
    ])
    cache = CachedMarketOverview(ttl_seconds=0)
    cache.refresh_cooldown_seconds = 0
    monkeypatch.setattr("tradebot.overview._overviews", cache)
    response = TestClient(create_app()).get(f"/api/overview/{market}?refresh=true")
    assert response.status_code == 200
    assert response.json()["assets"][0]["change"] == pytest.approx(expected)


def test_desktop_launcher_runs_current_checkout_instead_of_stale_install():
    launcher = Path("start-signal.command").read_text()
    assert 'export PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"' in launcher
    assert "exec .venv/bin/python -m tradebot.app" in launcher
    assert "exec signal-app" not in launcher


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


def test_manual_refresh_obeys_cooldown_and_returns_last_updated(monkeypatch):
    builds = []
    monkeypatch.setattr(MarketOverviewService, "build", lambda self, market:
                        builds.append(market) or {"market": market.value, "assets": []})
    overview = CachedMarketOverview(ttl_seconds=60)
    first = overview.get(MarketKind.EQUITIES)
    second = overview.get(MarketKind.EQUITIES, refresh=True)
    assert second is first
    assert len(builds) == 1
    assert first["last_updated"].endswith("+00:00")


def test_failed_manual_refresh_keeps_last_known_data(monkeypatch):
    overview = CachedMarketOverview(ttl_seconds=0)
    overview.refresh_cooldown_seconds = 0
    monkeypatch.setattr(MarketOverviewService, "build", lambda self, market:
                        {"market": market.value, "assets": [{"symbol": "AAPL"}]})
    original = overview.get(MarketKind.EQUITIES)
    monkeypatch.setattr(MarketOverviewService, "build", lambda self, market:
                        (_ for _ in ()).throw(ConnectionError("provider secret")))
    stale = overview.get(MarketKind.EQUITIES, refresh=True)
    assert stale["assets"] == original["assets"]
    assert stale["warning"] == "Couldn’t refresh right now. Showing last available data."
