from dataclasses import dataclass
from math import floor, isfinite

from .models import MarketSnapshot, OrderIntent, RiskDecision, Side, TradeSignal


@dataclass(frozen=True)
class RiskLimits:
    max_position_pct: float = 0.05
    max_order_notional: float = 5_000.0
    max_daily_loss_pct: float = 0.02
    min_confidence: float = 0.65


class RiskEngine:
    def __init__(self, limits: RiskLimits):
        self.limits = limits

    def evaluate(self, signal: TradeSignal, market: MarketSnapshot, equity: float,
                 current_quantity: int = 0, daily_pnl: float = 0.0) -> RiskDecision:
        if signal.side is Side.HOLD:
            return RiskDecision(False, "signal is HOLD", None)
        if signal.symbol != market.symbol:
            return RiskDecision(False, "signal/market symbol mismatch", None)
        if not isfinite(market.price) or market.price <= 0:
            return RiskDecision(False, "invalid reference price", None)
        if not 0 <= signal.confidence <= 1 or signal.confidence < self.limits.min_confidence:
            return RiskDecision(False, "confidence below threshold", None)
        if equity <= 0:
            return RiskDecision(False, "non-positive account equity", None)
        if daily_pnl <= -(equity * self.limits.max_daily_loss_pct):
            return RiskDecision(False, "daily loss circuit breaker active", None)

        position_cap = equity * self.limits.max_position_pct
        allowed_notional = min(position_cap, self.limits.max_order_notional)
        quantity = floor(allowed_notional / market.price)
        if signal.side in (Side.SELL, Side.STRONG_SELL):
            quantity = min(quantity, max(0, current_quantity))
        if quantity < 1:
            return RiskDecision(False, "order is below one share or exceeds holdings", None)

        execution_side = Side.BUY if signal.side is Side.STRONG_BUY else (
            Side.SELL if signal.side is Side.STRONG_SELL else signal.side)
        intent = OrderIntent.create(signal.symbol, execution_side, quantity, market.price, signal.confidence)
        return RiskDecision(True, "approved by deterministic limits", intent)
