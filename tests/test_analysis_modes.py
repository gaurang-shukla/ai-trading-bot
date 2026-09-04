import time
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from tradebot.analysis import DeepAnalysisCache, DeepJobRegistry, QuickSignalEngine, normalize_deep_reasoning
from tradebot.app import app
from tradebot.models import Candle, MarketSnapshot, Side, TradeSignal


def snapshot():
    return MarketSnapshot("BTCUSDT", 100.0, "2026-09-04T00:00:00Z", "WEEX",
                          4.2, 25_000_000, .0001, 5.0)


def wait_for_job(client, job_id, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = client.get(f"/api/analyze/deep/status/{job_id}").json()
        if result["status"] not in {"queued", "running"}:
            return result
        time.sleep(.01)
    raise AssertionError("job did not reach a terminal state")


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
        client = TestClient(app)
        started = client.post("/api/analyze/deep", json={"symbol": "BTCUSDT", "refresh": True}).json()
        result = wait_for_job(client, started["job_id"])
    assert result["status"] == "timed_out"
    assert result["user_friendly_error"].startswith("Deep AI reached")
    fallback = result["fallback_result"]
    assert {"signal", "live_price", "change_24h", "volume", "timeframe_breakdown", "key_levels"} <= fallback.keys()
    assert {"confidence", "risk_score", "stop_loss", "take_profit"} <= fallback["signal"].keys()
    assert "debug_error" not in result


def test_deep_timeout_raw_error_requires_debug(monkeypatch):
    provider = Mock(snapshot=Mock(return_value=snapshot()), candles=Mock(return_value=[]))
    registry = Mock(market_data=Mock(return_value=provider))
    monkeypatch.setenv("SIGNAL_DEEP_AI_TIMEOUT_SECONDS", "0.01")
    monkeypatch.setenv("SIGNAL_DEBUG", "true")
    with (patch("tradebot.app.default_registry", return_value=registry),
          patch("tradebot.app.signals.analyze", side_effect=lambda *_: time.sleep(.1))):
        client = TestClient(app)
        started = client.post("/api/analyze/deep", json={"symbol": "ETHUSDT", "refresh": True}).json()
        result = wait_for_job(client, started["job_id"])
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
    assert "still running in the background" in javascript
    assert "Check Deep AI status" in javascript
    assert "deepInsight(data)" in javascript
    assert 'id="quick-result"' in javascript


def test_background_registry_deduplicates_and_caches_completed_job():
    jobs = DeepJobRegistry(ttl_seconds=600)
    release = __import__("threading").Event()
    run = Mock(side_effect=lambda progress: (release.wait(), {"signal": {"side": "BUY"}})[1])
    first = jobs.start("crypto_futures", "SOLUSDT", run, lambda: {})
    second = jobs.start("crypto_futures", "solusdt", run, lambda: {}, refresh=True)
    assert first["job_id"] == second["job_id"]
    release.set()
    deadline = time.monotonic() + 1
    while jobs.get(first["job_id"])["status"] != "completed" and time.monotonic() < deadline:
        time.sleep(.01)
    cached = jobs.start("crypto_futures", "SOLUSDT", run, lambda: {})
    assert cached["job_id"] == first["job_id"]
    assert cached["cached"] is True
    assert run.call_count == 1


def test_chart_has_payload_and_unavailable_state():
    javascript = TestClient(app).get("/assets/app.js").text
    assert "chart_timeframes" in javascript
    assert "Chart unavailable" in javascript
    assert "candlestick price chart" in javascript


def test_chart_renders_candles_timeframes_and_collision_safe_labels():
    javascript = TestClient(app).get("/assets/app.js").text
    for frame in ("1m", "5m", "15m", "1h", "4h", "1d"):
        assert f"'{frame}'" in javascript
    assert 'class="candle ${up?' in javascript
    assert "placeChartLabels" in javascript
    assert "labelY+spacing" in javascript
    assert "Math.max(top,Math.min(bottom" in javascript
    assert "chart-label-pill" in javascript
    assert "current_price" in javascript


def test_chart_hover_tooltip_contains_ohlcv_and_bounded_positioning():
    javascript = TestClient(app).get("/assets/app.js").text
    assert 'class="chart-tooltip"' in javascript
    assert 'data-hover-enabled="true"' in javascript
    for field in ("time", "open", "high", "low", "close", "volume"):
        assert f'data-field="{field}"' in javascript
    assert "getBoundingClientRect()" in javascript
    assert "Math.max(gap,Math.min(left,stageBox.width-tip.width-gap))" in javascript
    assert "Math.max(gap,Math.min(top,stageBox.height-tip.height-gap))" in javascript
    assert "pointerdown" in javascript


def test_timeframe_switching_updates_and_remounts_advanced_chart():
    javascript = TestClient(app).get("/assets/app.js").text
    assert "function bindChartTimeframes" in javascript
    assert "container._chartCleanup?.()" in javascript
    assert "container.innerHTML=priceChart(data,button.dataset.chartFrame);bindChartTimeframes" in javascript
    assert "chart.timeScale().fitContent()" in javascript


def test_lightweight_chart_is_lazy_loaded_and_has_svg_fallback():
    javascript = TestClient(app).get("/assets/app.js").text
    html = TestClient(app).get("/").text
    assert "lightweight-charts@4.2.3" in javascript
    assert "loadLightweightCharts" in javascript
    assert "lightweight-chart-container" in javascript
    assert "svgPriceChart(data,frame)" in javascript
    assert "Advanced chart unavailable; showing basic chart." in javascript
    assert "lightweight-charts" not in html  # overview boot does not download the chart library


def test_lightweight_chart_maps_ohlc_volume_and_financial_features():
    javascript = TestClient(app).get("/assets/app.js").text
    assert "function mapCandlesToLightweight" in javascript
    assert "time:chartTime" in javascript
    for field in ("open:Number", "high:Number", "low:Number", "close:Number"):
        assert field in javascript
    assert "function mapVolumeToLightweight" in javascript
    assert "chart.addHistogramSeries" in javascript
    assert "chart.addCandlestickSeries" in javascript
    assert "subscribeCrosshairMove" in javascript
    assert "rightPriceScale:{visible:true" in javascript
    assert "timeScale:{visible:true" in javascript
    assert "new ResizeObserver" in javascript


def test_price_lines_protect_scale_and_all_timeframes_are_present():
    javascript = TestClient(app).get("/assets/app.js").text
    assert "function priceLineConfiguration" in javascript
    for level in ("Support", "Resistance", "Stop", "Take profit", "Current"):
        assert f"['{level}'" in javascript
    assert "levels.filter(level=>level.inScale)" in javascript
    assert "off scale" in javascript
    assert "data-chart-frame" in javascript and "?'':'disabled'" in javascript


def test_deep_completed_and_fallback_views_stay_compact_and_compare_signals():
    javascript = TestClient(app).get("/assets/app.js").text
    completed = javascript.split("if(job.status==='completed')", 1)[1].split("return}const fallback", 1)[0]
    assert "deepInsight(data)" in completed
    assert "signalPanel(" not in completed
    assert "technicalPanels(" not in completed
    assert "chart-mount" not in completed
    assert "live-price" not in completed
    assert "Deep AI ${agrees?'agrees with':'differs from'} Quick Signal" in javascript
    assert "Quick Signal:" in javascript and "Deep AI:" in javascript
    assert "Reason for difference" in javascript
    assert "Deep AI fallback used." in javascript
    assert "The main Quick Signal above remains the source of this result." in javascript


def test_quick_api_reuses_ohlcv_for_chart_without_extra_fetch():
    provider = Mock()
    provider.snapshot.return_value = snapshot()
    provider.candles.side_effect = lambda _symbol, frame, _limit: [
        Candle(i, 100 + i, 102 + i, 99 + i, 101 + i, 1_000 + i)
        for i in range(30)
    ]
    registry = Mock()
    registry.market_data.return_value = provider
    with patch("tradebot.app.default_registry", return_value=registry):
        response = TestClient(app).post("/api/analyze/quick", json={"symbol": "CHARTTESTUSDT"})
    assert response.status_code == 200
    result = response.json()
    assert set(result["chart_timeframes"]) == {"1m", "5m", "15m", "1h", "4h", "1d"}
    assert result["chart_default_timeframe"] == "1h"
    candle = result["chart_timeframes"]["1h"][0]
    assert {"timestamp", "open", "high", "low", "close", "volume"} == set(candle)
    assert provider.candles.call_count == 6


def test_empty_candles_keep_live_price_card_and_chart_unavailable():
    javascript = TestClient(app).get("/assets/app.js").text
    assert 'class="live-price"' in javascript
    assert "data.live_price??marketData.price" in javascript
    assert "bars.length<2" in javascript
    assert "Chart unavailable" in javascript
