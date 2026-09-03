from dataclasses import asdict

from .adapters import MarketData, PaperclipReporter, SignalProvider
from .execution import PaperBroker
from .risk import RiskEngine


class TradingService:
    def __init__(self, data: MarketData, signals: SignalProvider, risk: RiskEngine,
                 broker: PaperBroker, reporter: PaperclipReporter | None = None):
        self.data, self.signals, self.risk, self.broker = data, signals, risk, broker
        self.reporter = reporter

    def run(self, symbol: str, as_of: str, equity: float) -> dict:
        market = self.data.snapshot(symbol.upper())
        signal = self.signals.analyze(symbol.upper(), as_of)
        decision = self.risk.evaluate(signal, market, equity,
                                      self.broker.positions.get(symbol.upper(), 0))
        result = {"market": asdict(market), "signal": asdict(signal), "risk": asdict(decision)}
        if decision.approved and decision.intent:
            result["execution"] = self.broker.execute(decision.intent).to_dict()
        if self.reporter:
            self.reporter.report(result)
        return result

