from dataclasses import asdict
from datetime import datetime, timezone

from .adapters import MarketData, PaperclipReporter, SignalProvider
from .execution import PaperBroker
from .risk import RiskEngine
from .models import MarketSnapshot, Side, TradeSignal


class TradingService:
    def __init__(self, data: MarketData, signals: SignalProvider, risk: RiskEngine,
                 broker: PaperBroker, reporter: PaperclipReporter | None = None):
        self.data, self.signals, self.risk, self.broker = data, signals, risk, broker
        self.reporter = reporter

    def run(self, symbol: str, as_of: str, equity: float) -> dict:
        exchange_symbol = symbol.upper()
        errors = []
        try:
            market = self.data.snapshot(exchange_symbol)
        except Exception as exc:
            errors.append(f"Market data unavailable: {type(exc).__name__}: {exc}")
            market = MarketSnapshot(exchange_symbol, 0.0, datetime.now(timezone.utc).isoformat(), "unavailable")
        try:
            signal = self.signals.analyze(exchange_symbol, as_of)
        except Exception as exc:
            errors.append(f"Signal analysis unavailable: {type(exc).__name__}: {exc}")
            signal = TradeSignal(exchange_symbol, Side.HOLD, 0.0, errors[-1], "safe_fallback")
        decision = self.risk.evaluate(signal, market, equity,
                                      self.broker.positions.get(symbol.upper(), 0))
        result = {"market": asdict(market), "signal": asdict(signal), "risk": asdict(decision)}
        if errors:
            result["warnings"] = errors
        if decision.approved and decision.intent:
            result["execution"] = self.broker.execute(decision.intent).to_dict()
        if self.reporter:
            try:
                self.reporter.report(result)
            except Exception as exc:
                result.setdefault("warnings", []).append(
                    f"Reporting unavailable: {type(exc).__name__}: {exc}")
        return result
