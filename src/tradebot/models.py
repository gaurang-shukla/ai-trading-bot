from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class Side(str, Enum):
    STRONG_BUY = "STRONG BUY"
    BUY = "BUY"
    STRONG_SELL = "STRONG SELL"
    SELL = "SELL"
    HOLD = "HOLD"


class MarketKind(str, Enum):
    CRYPTO_SPOT = "crypto_spot"
    CRYPTO_FUTURES = "crypto_futures"
    EQUITIES = "equities"
    FOREX = "forex"
    COMMODITIES = "commodities"
    OPTIONS = "options"
    INDIAN_INDICES = "indian_indices"
    BANKNIFTY_OPTIONS = "banknifty_options"


@dataclass(frozen=True)
class MarketSelection:
    market: MarketKind
    venue: str
    symbol: str
    quote_currency: str = "USD"


@dataclass(frozen=True)
class MarketSnapshot:
    symbol: str
    price: float
    as_of: str
    source: str
    change_24h: float | None = None
    volume: float | None = None
    funding_rate: float | None = None
    volatility_24h: float | None = None


@dataclass(frozen=True)
class Candle:
    """Provider-neutral OHLCV bar (volume is optional for FX/index feeds)."""

    timestamp: int | str
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None


@dataclass(frozen=True)
class IndexOption:
    expiry: str | None
    strike: float
    option_type: str
    last_price: float | None
    change: float | None
    volume: float | None
    open_interest: float | None
    implied_volatility: float | None
    delta: float | None
    gamma: float | None
    theta: float | None
    vega: float | None
    bid: float | None
    ask: float | None
    underlying_price: float
    moneyness: str
    distance_from_spot_pct: float


@dataclass(frozen=True)
class TradeSignal:
    symbol: str
    side: Side
    confidence: float
    rationale: str
    model: str
    risk_score: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    probability: float | None = None
    position_size_pct: float | None = None


@dataclass(frozen=True)
class OrderIntent:
    symbol: str
    side: Side
    quantity: int
    reference_price: float
    signal_confidence: float
    created_at: str

    @classmethod
    def create(cls, symbol: str, side: Side, quantity: int, price: float, confidence: float):
        return cls(symbol, side, quantity, price, confidence, datetime.now(timezone.utc).isoformat())


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    reason: str
    intent: OrderIntent | None


@dataclass(frozen=True)
class ExecutionReport:
    order_id: str
    status: str
    symbol: str
    side: Side
    quantity: int
    fill_price: float
    mode: str = "paper"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
