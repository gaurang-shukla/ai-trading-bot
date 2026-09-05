import time
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from tradebot.app import app
from tradebot.models import MarketSnapshot
from tradebot.venues import default_registry
from tradebot.weex import live_execution_enabled


client = TestClient(app)
MARKETS = {
    "crypto_futures", "crypto_spot", "equities", "forex", "commodities",
    "indian_indices", "banknifty_options",
}
MARKETSETS = sorted(MARKETS)


def test_all_supported_market_and_asset_pages_have_safe_spa_routes():
    for market in MARKETSETS:
        response = client.get(f"/market/{market}")
        assert response.status_code == 200, market
        assert '<main id="app"' in response.text
        if market != "banknifty_options":
            asset = client.get(f"/asset/{market}/TEST")
            assert asset.status_code == 200, market
            assert '<main id="app"' in asset.text


def test_market_registry_contains_every_public_market_and_hides_generic_options():
    public = {item["market"] for item in client.get("/api/markets").json()}
    assert MARKETS == public
    assert "options" not in public
    javascript = client.get("/assets/app.js").text
    assert "market==='options'" in javascript
    assert "NOT CONNECTED YET" in javascript


def test_provider_errors_are_redacted_unless_debug_is_enabled(monkeypatch):
    secret = "provider-token-super-secret"
    with patch("tradebot.app.market_overview", side_effect=RuntimeError(secret)):
        monkeypatch.delenv("SIGNAL_DEBUG", raising=False)
        response = client.get("/api/overview/equities")
        assert response.status_code == 502
        assert secret not in response.text
        monkeypatch.setenv("SIGNAL_DEBUG", "true")
        assert secret in client.get("/api/overview/equities").text

    with patch("tradebot.app.OpenBBClient.option_chain", side_effect=RuntimeError(secret)):
        monkeypatch.delenv("SIGNAL_DEBUG", raising=False)
        assert secret not in client.get("/api/options/TEST").text


def test_quick_signal_returns_partial_result_without_waiting_for_slow_candles(monkeypatch):
    provider = Mock()
    provider.snapshot.return_value = MarketSnapshot(
        "BTCUSDT", 100, "2026-09-04T00:00:00Z", "test", change_24h=1,
    )
    provider.candles.side_effect = lambda *_: (time.sleep(.25), [])[1]
    registry = Mock(market_data=Mock(return_value=provider))
    monkeypatch.setenv("SIGNAL_QUICK_CANDLE_TIMEOUT_SECONDS", "0.05")
    started = time.perf_counter()
    with patch("tradebot.app.default_registry", return_value=registry):
        response = client.post("/api/analyze/quick", json={"symbol": "BTCUSDT"})
    assert time.perf_counter() - started < .2
    assert response.status_code == 200
    assert response.json()["fallback"] is True
    assert any("timeout" in warning for warning in response.json()["warnings"])


def test_deep_job_submission_does_not_run_work_in_request_thread():
    release = __import__("threading").Event()
    provider = Mock(
        snapshot=Mock(return_value=MarketSnapshot("NONBLOCK", 100, "now", "test")),
        candles=Mock(return_value=[]),
    )
    registry = Mock(market_data=Mock(return_value=provider))
    with (patch("tradebot.app.default_registry", return_value=registry),
          patch("tradebot.app.quick_signals.analyze", side_effect=lambda *_: release.wait(1))):
        started = time.perf_counter()
        response = client.post("/api/analyze/deep", json={"symbol": "NONBLOCK", "refresh": True})
        elapsed = time.perf_counter() - started
    release.set()
    assert response.status_code == 200
    assert response.json()["status"] in {"queued", "running"}
    assert elapsed < .2


def test_chart_dependency_failure_keeps_basic_chart_fallback():
    javascript = client.get("/assets/app.js").text
    assert "svgPriceChart(data,frame)" in javascript
    assert "Advanced chart unavailable; showing basic chart." in javascript
    assert "lightweightChartsPromise=null" in javascript


def test_paper_is_the_only_application_execution_mode(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "live")
    monkeypatch.setenv("WEEX_LIVE_ENABLED", "true")
    assert client.get("/api/status").json()["mode"] == "paper"
    assert live_execution_enabled() is False
    assert default_registry().choices()


def test_paperclip_status_requires_a_real_bridge_configuration(monkeypatch):
    from tradebot.app import integration_status

    for key in ("PAPERCLIP_TASK_BRIDGE_URL", "PAPERCLIP_API_KEY", "PAPERCLIP_BRIDGE_TOKEN",
                "PAPERCLIP_ENABLED", "PAPERCLIP_API_URL"):
        monkeypatch.delenv(key, raising=False)
    status = integration_status()["paperclip"]
    assert status["configured"] is False
    assert status["ready"] is False
    assert status["enabled"] is False
