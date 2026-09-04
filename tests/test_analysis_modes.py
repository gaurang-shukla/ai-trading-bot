import time
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from tradebot.analysis import DeepAnalysisCache, QuickSignalEngine
from tradebot.app import app
from tradebot.models import MarketSnapshot


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
