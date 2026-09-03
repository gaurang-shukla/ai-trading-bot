import uuid

from .models import ExecutionReport, OrderIntent, Side


class PaperBroker:
    def __init__(self, starting_cash: float = 100_000.0):
        self.cash = starting_cash
        self.positions: dict[str, int] = {}

    @property
    def mode(self) -> str:
        return "paper"

    def execute(self, intent: OrderIntent) -> ExecutionReport:
        notional = intent.quantity * intent.reference_price
        held = self.positions.get(intent.symbol, 0)
        if intent.side is Side.BUY:
            if notional > self.cash:
                raise ValueError("insufficient paper cash")
            self.cash -= notional
            self.positions[intent.symbol] = held + intent.quantity
        elif intent.side is Side.SELL:
            if intent.quantity > held:
                raise ValueError("insufficient paper position")
            self.cash += notional
            self.positions[intent.symbol] = held - intent.quantity
        else:
            raise ValueError("HOLD cannot be executed")
        return ExecutionReport(str(uuid.uuid4()), "FILLED", intent.symbol, intent.side,
                               intent.quantity, intent.reference_price)

