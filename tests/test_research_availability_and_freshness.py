from datetime import datetime, timezone
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from tradebot.app import (AnalyzeRequest, _deep_failure_from_notice,
                          advanced_research_availability, app)
from tradebot.models import MarketKind, MarketSnapshot


def availability(market, symbol, quick=None):
    request = AnalyzeRequest(market=market, symbol=symbol)
    return advanced_research_availability(request, quick or {"signal": {"side": "HOLD"}})


def test_supported_and_exchange_only_crypto_have_predictable_availability():
    link = availability(MarketKind.CRYPTO_FUTURES, "LINKUSDT")
    assert link["advanced_research_available"] is True
    assert link["availability_status"] == "available"
    assert link["recommended_action"] == "Run Advanced Research"
    for symbol in ("STONKUSDT", "DUSTUSDT"):
        state = availability(MarketKind.CRYPTO_SPOT, symbol)
        assert state["advanced_research_available"] is False
        assert state["availability_status"] == "unsupported_symbol"
        assert state["can_retry"] is False
        assert state["recommended_action"] == "Use Quick Signal and Fast AI Explanation"
        assert "external symbol" in state["availability_reason"]


def test_temporary_provider_failure_is_retryable():
    failure = _deep_failure_from_notice("provider connection unavailable", "LINK-USD").deep_failure
    assert failure["failure_category"] == "provider_unavailable"
    assert failure["can_retry"] is True


def test_gold_and_banknifty_have_specific_preflight_states():
    gold = availability(MarketKind.COMMODITIES, "GC")
    assert gold["failure_category"] == "unsupported_market"
    assert "commodity symbol" in gold["availability_reason"]
    assert "TradingAgents" in gold["availability_reason"]
    bank = availability(MarketKind.INDIAN_INDICES, "BANKNIFTY")
    assert bank["availability_status"] == "provider_required"
    assert bank["recommended_action"] == "Connect a live option-chain provider"


def test_quick_refresh_records_completion_time_separately_from_old_provider_time():
    old_market_time = "2020-01-01T00:00:00Z"
    provider = Mock(
        snapshot=Mock(return_value=MarketSnapshot("LINKUSDT", 10, old_market_time, "test", 1, 1000)),
        candles=Mock(return_value=[]),
    )
    registry = Mock(market_data=Mock(return_value=provider))
    before = datetime.now(timezone.utc)
    with patch("tradebot.app.default_registry", return_value=registry):
        payload = TestClient(app).post("/api/analyze/quick", json={
            "market": "crypto_futures", "symbol": "LINKUSDT", "refresh": True
        }).json()
    refreshed = datetime.fromisoformat(payload["setup_refreshed_at"])
    assert refreshed >= before
    assert payload["provider_data_timestamp"] == old_market_time
    assert payload["stale_after_seconds"] == 120


def test_frontend_uses_setup_refresh_not_candle_time_and_uniform_deep_controls():
    javascript = TestClient(app).get("/assets/app.js").text
    assert "Date.parse(data.setup_refreshed_at" in javascript
    assert "Number(data.stale_after_seconds)" in javascript
    assert "Market data timestamp:" in javascript
    assert "Could not refresh latest market data. Showing last known setup." in javascript
    assert "Use Quick Signal and Fast AI Explanation." in javascript
    assert "state.can_retry?'<button id=\"deep-button\"" in javascript
    assert "restart-deep" not in javascript
    running = javascript.split("function runningMarkup", 1)[1].split("function deepInsight", 1)[0]
    assert "Use Quick Signal" in running
    assert "Run Advanced Research" not in running
    assert "SIDE.HOLD" not in javascript


def test_market_freshness_thresholds_are_explicit():
    javascript = TestClient(app).get("/assets/app.js").text
    assert "crypto_spot:120,crypto_futures:120" in javascript
    assert "forex:600,commodities:600,equities:900,indian_indices:900" in javascript


def test_expired_deep_job_is_a_calm_structured_state(monkeypatch):
    monkeypatch.setenv("SIGNAL_DEBUG", "false")
    payload = TestClient(app).get("/api/analyze/deep/status/expired-id").json()
    assert payload["status"] == "expired_job"
    assert payload["user_friendly_error"] == "Advanced Research session expired."
    assert "debug_error" not in payload
    javascript = TestClient(app).get("/assets/app.js").text
    assert "sessionStorage.removeItem(`deep-job:${key}`)" in javascript
    assert "Advanced Research session expired." in javascript


def test_expired_deep_job_raw_detail_is_debug_only(monkeypatch):
    monkeypatch.setenv("SIGNAL_DEBUG", "true")
    payload = TestClient(app).get("/api/analyze/deep/status/expired-id").json()
    assert payload["debug_error"] == "Deep AI job not found"
