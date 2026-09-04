from tradebot.execution import PaperBroker
from tradebot.models import MarketSnapshot, Side, TradeSignal
from tradebot.models import MarketKind, MarketSelection
from tradebot.risk import RiskEngine, RiskLimits
from tradebot.service import TradingService
from tradebot.venues import default_registry
from tradebot.adapters import (CachedSignalProvider, FallbackMarketData, NormalizedMarketData,
                               TradingAgentsClient, _import_attribute,
                               research_symbol)


class FakeData:
    def snapshot(self, symbol):
        return MarketSnapshot(symbol, 100.0, "2026-09-01", "test")


class FakeSignals:
    def __init__(self, side=Side.BUY, confidence=0.8):
        self.side, self.confidence = side, confidence

    def analyze(self, symbol, as_of):
        return TradeSignal(symbol, self.side, self.confidence, "test signal", "fake")


def make_service(signals=FakeSignals()):
    return TradingService(FakeData(), signals, RiskEngine(RiskLimits()), PaperBroker())


def test_approved_buy_is_capped_and_filled():
    result = make_service().run("AAPL", "2026-09-01", 100_000)
    assert result["execution"]["status"] == "FILLED"
    assert result["execution"]["quantity"] == 50
    assert result["execution"]["mode"] == "paper"


def test_low_confidence_is_rejected():
    result = make_service(FakeSignals(confidence=0.5)).run("AAPL", "2026-09-01", 100_000)
    assert result["risk"]["approved"] is False
    assert "execution" not in result


def test_hold_is_never_executed():
    result = make_service(FakeSignals(side=Side.HOLD)).run("AAPL", "2026-09-01", 100_000)
    assert result["risk"]["approved"] is False
    assert "execution" not in result


def test_sell_cannot_exceed_holdings():
    service = make_service(FakeSignals(side=Side.SELL))
    result = service.run("AAPL", "2026-09-01", 100_000)
    assert result["risk"]["approved"] is False


def test_user_can_select_weex_spot_or_futures():
    registry = default_registry()
    spot = registry.market_data(MarketSelection(MarketKind.CRYPTO_SPOT, "weex", "BTCUSDT"))
    futures = registry.market_data(MarketSelection(MarketKind.CRYPTO_FUTURES, "weex", "BTCUSDT"))
    assert isinstance(spot, FallbackMarketData)
    assert isinstance(futures, FallbackMarketData)


def test_weex_symbols_are_normalized_only_for_research_providers():
    assert research_symbol("BTCUSDT") == "BTC-USD"
    assert research_symbol("ethusdt") == "ETH-USD"
    assert research_symbol("XRPUSDT") == "XRP-USD"
    assert research_symbol("AAPL") == "AAPL"

    class CapturingProvider:
        requested = None

        def snapshot(self, symbol):
            self.requested = symbol
            return MarketSnapshot(symbol, 100, "2026-09-01", "research")

    provider = CapturingProvider()
    snapshot = NormalizedMarketData(provider).snapshot("BTCUSDT")
    assert provider.requested == "BTC-USD"
    assert snapshot.symbol == "BTCUSDT"


def test_signal_failure_becomes_a_safe_hold(monkeypatch):
    monkeypatch.setattr("tradebot.adapters._import_attribute",
                        lambda candidates: (_ for _ in ()).throw(ImportError("missing")))
    signal = TradingAgentsClient().analyze("BTCUSDT", "2026-09-01")
    assert signal.side is Side.HOLD
    assert signal.symbol == "BTCUSDT"
    assert signal.model == "safe_fallback"
    assert signal.rationale == "AI temporarily unavailable. Showing live market data only."


def test_analysis_cache_reuses_result_within_ttl():
    provider = FakeSignals()
    provider.calls = 0
    original = provider.analyze
    provider.analyze = lambda *args: (setattr(provider, "calls", provider.calls + 1) or original(*args))
    cached = CachedSignalProvider(provider, ttl_seconds=600)
    assert cached.analyze("AAPL", "2026-09-01") is cached.analyze("aapl", "2026-09-01")
    assert provider.calls == 1


def test_analysis_cache_expires(monkeypatch):
    provider = FakeSignals()
    cached = CachedSignalProvider(provider, ttl_seconds=5)
    clock = iter((10.0, 20.0))
    monkeypatch.setattr("tradebot.adapters.time.monotonic", lambda: next(clock))
    first = cached.analyze("AAPL", "2026-09-01")
    second = cached.analyze("AAPL", "2026-09-01")
    assert first is not second


def test_compatible_import_skips_missing_package_layouts(monkeypatch):
    class CompatibleModule:
        DEFAULT_CONFIG = {"layout": "compatible"}

    def import_module(name):
        if name == "tradingagents.config":
            return CompatibleModule
        raise ImportError(name)

    monkeypatch.setattr("tradebot.adapters.importlib.import_module", import_module)
    result = _import_attribute((
        ("tradingagents.default_config", "DEFAULT_CONFIG"),
        ("tradingagents.config", "DEFAULT_CONFIG"),
    ))
    assert result == {"layout": "compatible"}


def test_market_data_failure_does_not_crash_analysis():
    class BrokenData:
        def snapshot(self, symbol):
            raise ConnectionError("all feeds down")

    service = TradingService(BrokenData(), FakeSignals(), RiskEngine(RiskLimits()), PaperBroker())
    result = service.run("BTCUSDT", "2026-09-01", 100_000)
    assert result["market"]["source"] == "unavailable"
    assert result["risk"]["approved"] is False
    assert result["warnings"]


def test_openai_failure_keeps_live_market_data_visible():
    class BrokenSignals:
        def analyze(self, symbol, as_of):
            raise RuntimeError("429 Rate limit exceeded")
    result = make_service(BrokenSignals()).run("AAPL", "2026-09-01", 100_000)
    assert result["market"]["price"] == 100
    assert result["signal"]["side"] is Side.HOLD


def test_non_crypto_markets_are_exposed_through_openbb():
    choices = default_registry().choices()
    assert {item["market"] for item in choices} >= {"equities", "forex", "commodities", "options"}
