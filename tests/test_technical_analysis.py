from tradebot.analysis import QuickSignalEngine, score_ema_trend, score_macd
from tradebot.indicators import atr, bollinger_bands, ema, macd, rsi, support_resistance, vwap
from tradebot.models import Candle, MarketSnapshot


def candles(count=240, step=.2):
    return [Candle(i, 100 + i * step, 101 + i * step, 99 + i * step,
                   100.5 + i * step, 1_000 + i) for i in range(count)]


def test_indicator_calculations_have_aligned_outputs():
    bars = candles()
    closes = [bar.close for bar in bars]
    assert len(ema(closes, 200)) == len(bars)
    assert ema(closes, 9)[7] is None
    assert rsi(closes)[-1] == 100
    values = macd(closes)
    assert values["line"][-1] > values["signal"][-1]
    assert set(bollinger_bands(closes)) == {"middle", "upper", "lower"}
    assert atr(bars)[-1] > 0
    assert vwap(bars)[-1] is not None
    assert support_resistance(bars)["support"] is not None


def test_rsi_edge_cases_are_defined_and_bounded():
    assert rsi([10.0] * 20)[-1] == 50
    assert rsi(list(range(20)))[-1] == 100
    assert rsi(list(range(20, 0, -1)))[-1] == 0
    assert all(value is None or 0 <= value <= 100 for value in rsi(candles_to_closes(candles())))


def candles_to_closes(bars):
    return [bar.close for bar in bars]


def test_ema_and_macd_scoring_direction():
    assert score_ema_trend(110, 108, 105, 100, 90) > 0
    assert score_ema_trend(90, 92, 95, 100, 110) < 0
    assert score_macd(2, 1, .5) > 0
    assert score_macd(-2, -1, -.5) < 0


def test_missing_candles_falls_back_and_schema_is_stable():
    market = MarketSnapshot("BTCUSDT", 100, "now", "test", 2, 1000, .0001, 3)
    result = QuickSignalEngine().analyze(market, 10_000, {})
    assert result["fallback"] is True
    assert result["signal"]["model"] == "quick_rules_v1"
    assert set(("trend_summary", "momentum_summary", "volatility_summary", "key_levels",
                "timeframe_breakdown", "plain_language_reason")) <= result.keys()


def test_multi_timeframe_score_produces_stable_schema():
    market = MarketSnapshot("BTCUSDT", candles()[-1].close, "now", "test", 2, 1000, .0001, 3)
    result = QuickSignalEngine().analyze(market, 10_000, {frame: candles() for frame in ("1m", "1h", "1d")})
    assert result["fallback"] is False
    assert result["signal"]["model"] == "multi_timeframe_ta_v1"
    assert result["signal"]["side"] in {"STRONG BUY", "BUY", "HOLD", "SELL", "STRONG SELL"}
    assert len(result["timeframe_breakdown"]) == 3
    assert set(result["timeframe_breakdown"][0]) == {"timeframe", "trend", "rsi", "macd", "ema_bias", "score"}
