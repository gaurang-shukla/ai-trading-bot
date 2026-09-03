from tradebot.execution import PaperBroker
from tradebot.models import MarketSnapshot, Side, TradeSignal
from tradebot.models import MarketKind, MarketSelection
from tradebot.risk import RiskEngine, RiskLimits
from tradebot.service import TradingService
from tradebot.venues import default_registry
from tradebot.adapters import WeexFuturesMarketData, WeexSpotMarketData


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
    assert isinstance(spot, WeexSpotMarketData)
    assert isinstance(futures, WeexFuturesMarketData)


def test_non_crypto_markets_are_exposed_through_openbb():
    choices = default_registry().choices()
    assert {item["market"] for item in choices} >= {"equities", "forex", "commodities", "options"}
