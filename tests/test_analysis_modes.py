import time
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from tradebot.analysis import DeepAnalysisCache, QuickSignalEngine, normalize_deep_reasoning
from tradebot.app import app
from tradebot.models import MarketSnapshot, Side, TradeSignal


def snapshot():
    return MarketSnapshot("BTCUSDT", 100.0, "2026-09-04T00:00:00Z", "WEEX",
                          4.2, 25_000_000, .0001, 5.0)


def test_quick_signal_does_not_call_openai_or_tradingagents():
    forbidden = Mock(side_effect=AssertionError("AI must not run on the quick path"))
    with patch("tradebot.adapters.TradingAgentsClient.analyze", forbidden):
        result = QuickSignalEngine().analyze(snapshot(), 100_000)
    assert result["mode"] == "quick"
    assert result["signal"]["model"] == "quick_rules_v1"
    assert result["signal"]["side"] in {"BUY", "SELL", "HOLD", "STRONG BUY", "STRONG SELL"}
    forbidden.assert_not_called()


def test_quick_signal_computation_uses_fast_path():
    started = time.perf_counter()
    for _ in range(1_000):
        result = QuickSignalEngine().analyze(snapshot(), 100_000)
    assert time.perf_counter() - started < 1.0
    assert result["signal"]["probability"] is not None
    assert result["signal"]["stop_loss"] is not None


def test_deep_results_are_cached_by_market_and_symbol():
    cache = DeepAnalysisCache(ttl_seconds=600)
    run = Mock(return_value={"ai_available": True, "signal": {"side": "BUY"}})
    first = cache.get_or_run("crypto_futures", "BTCUSDT", run)
    second = cache.get_or_run("crypto_futures", "btcusdt", run)
    cache.get_or_run("crypto_spot", "BTCUSDT", run)
    assert first["cached"] is False
    assert second["cached"] is True
    assert second["deep_analyzed_at"] == first["deep_analyzed_at"]
    assert run.call_count == 2


def test_deep_ai_failure_does_not_replace_quick_signal_ui():
    client = TestClient(app)
    page = client.get("/").text
    javascript = client.get("/assets/app.js").text
    assert "Run Deep AI Research" in javascript
    assert "Quick Signal remains available above" in javascript
    assert "id=\"quick-result\"" in javascript
    assert "Signal" in page


def test_deep_timeout_returns_complete_quick_fallback(monkeypatch):
    provider = Mock()
    provider.snapshot.return_value = snapshot()
    provider.candles.return_value = []
    registry = Mock()
    registry.market_data.return_value = provider

    def slow_signal(*_args):
        time.sleep(.1)
        return TradeSignal("BTCUSDT", Side.HOLD, .5, "Hold", "test")

    monkeypatch.setenv("SIGNAL_DEEP_AI_TIMEOUT_SECONDS", "0.01")
    monkeypatch.delenv("SIGNAL_DEBUG", raising=False)
    with (patch("tradebot.app.default_registry", return_value=registry),
          patch("tradebot.app.signals.analyze", side_effect=slow_signal)):
        result = TestClient(app).post("/api/analyze/deep", json={"symbol": "BTCUSDT", "refresh": True}).json()
    assert result["timed_out"] is True
    assert result["ai_notice"].startswith("Deep AI took too long")
    assert {"signal", "live_price", "change_24h", "volume", "timeframe_breakdown", "key_levels"} <= result.keys()
    assert {"confidence", "risk_score", "stop_loss", "take_profit"} <= result["signal"].keys()
    assert "debug_error" not in result


def test_deep_timeout_raw_error_requires_debug(monkeypatch):
    provider = Mock(snapshot=Mock(return_value=snapshot()), candles=Mock(return_value=[]))
    registry = Mock(market_data=Mock(return_value=provider))
    monkeypatch.setenv("SIGNAL_DEEP_AI_TIMEOUT_SECONDS", "0.01")
    monkeypatch.setenv("SIGNAL_DEBUG", "true")
    with (patch("tradebot.app.default_registry", return_value=registry),
          patch("tradebot.app.signals.analyze", side_effect=lambda *_: time.sleep(.1))):
        result = TestClient(app).post("/api/analyze/deep", json={"symbol": "BTCUSDT", "refresh": True}).json()
    assert "TimeoutError" in result["debug_error"]


def test_hold_reasoning_is_expanded_from_quick_signal():
    quick = QuickSignalEngine().analyze(snapshot(), 100_000)
    quick.update({"live_price": 100, "change_24h": 4.2, "volume": 25_000_000})
    deep = {"signal": {"side": "HOLD", "rationale": "Hold", "risk_score": .4,
                       "position_size_pct": 0, "stop_loss": None, "take_profit": None}}
    result = normalize_deep_reasoning(deep, quick)
    reason = result["plain_language_reason"]
    for section in ("Decision summary:", "Multi-timeframe confirmation:", "Indicators:",
                    "Risk guidance:", "Beginner-friendly meaning:", "What to watch next:"):
        assert section in reason
    assert reason != "Hold"


def test_deep_frontend_has_progress_timeout_and_preserves_quick_result():
    javascript = TestClient(app).get("/assets/app.js").text
    for step in ("Preparing market data", "Checking technical indicators", "Running TradingAgents research",
                 "Building plain-language summary", "Finalizing decision"):
        assert step in javascript
    assert "controller.abort()" in javascript
    assert "signalPanel(data" in javascript and "technicalPanels(data)" in javascript
    assert 'id="quick-result"' in javascript
