import importlib
import json
import os
from datetime import date, datetime, timezone
from typing import Protocol
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .models import MarketSnapshot, Side, TradeSignal


_CRYPTO_QUOTES = ("USDT", "USDC", "USD")


def research_symbol(symbol: str) -> str:
    """Translate an exchange crypto pair into the format used by research feeds."""
    value = symbol.upper().replace("/", "").replace(":", "")
    if "-" in value:
        return value
    for quote in _CRYPTO_QUOTES:
        if value.endswith(quote) and len(value) > len(quote):
            return f"{value[:-len(quote)]}-USD"
    return value


class MarketData(Protocol):
    def snapshot(self, symbol: str) -> MarketSnapshot: ...


class SignalProvider(Protocol):
    def analyze(self, symbol: str, as_of: str) -> TradeSignal: ...


class OpenBBClient:
    """OpenBB remains a separate service; this adapter consumes its public API."""

    def __init__(self, base_url: str | None = None):
        self.base_url = (base_url or os.getenv("OPENBB_API_URL", "http://127.0.0.1:6900")).rstrip("/")

    def snapshot(self, symbol: str) -> MarketSnapshot:
        url = f"{self.base_url}/api/v1/equity/price/quote?symbol={symbol}"
        with urlopen(url, timeout=20) as response:
            payload = json.load(response)
        row = payload["results"][0]
        price = row.get("last_price") or row.get("price") or row.get("close")
        return MarketSnapshot(symbol.upper(), float(price), row.get("last_trade_timestamp", date.today().isoformat()), "openbb")


class YahooFinanceClient:
    """Optional yfinance adapter; importing it lazily keeps Yahoo non-essential."""

    def snapshot(self, symbol: str) -> MarketSnapshot:
        yfinance = importlib.import_module("yfinance")
        ticker = yfinance.Ticker(symbol)
        history = ticker.history(period="2d", interval="1d")
        if history.empty:
            raise ValueError(f"Yahoo Finance returned no quote for {symbol}")
        row = history.iloc[-1]
        timestamp = getattr(history.index[-1], "isoformat", lambda: date.today().isoformat())()
        return MarketSnapshot(symbol.upper(), float(row["Close"]), timestamp, "yahoo_finance")


class NormalizedMarketData:
    """Query a research provider with its symbol while retaining the WEEX identity."""

    def __init__(self, provider: MarketData):
        self.provider = provider

    def snapshot(self, symbol: str) -> MarketSnapshot:
        result = self.provider.snapshot(research_symbol(symbol))
        return MarketSnapshot(symbol.upper(), result.price, result.as_of, result.source)


class FallbackMarketData:
    def __init__(self, *providers: MarketData):
        self.providers = providers

    def snapshot(self, symbol: str) -> MarketSnapshot:
        errors = []
        for provider in self.providers:
            try:
                return provider.snapshot(symbol)
            except Exception as exc:
                errors.append(f"{type(provider).__name__}: {exc}")
        raise RuntimeError("; ".join(errors) or "no market data providers configured")


class _WeexPublicClient:
    base_url: str

    def _get(self, path: str, params: dict[str, str]) -> dict | list:
        url = f"{self.base_url}{path}?{urlencode(params)}"
        request = Request(url, headers={"User-Agent": "three-layer-tradebot/0.2"})
        with urlopen(request, timeout=10) as response:
            return json.load(response)

    @staticmethod
    def _timestamp(value: int | str | None) -> str:
        if value is None:
            return datetime.now(timezone.utc).isoformat()
        return datetime.fromtimestamp(int(value) / 1000, timezone.utc).isoformat()


class WeexSpotMarketData(_WeexPublicClient):
    base_url = "https://api-spot.weex.com"

    def snapshot(self, symbol: str) -> MarketSnapshot:
        payload = self._get("/api/v3/market/ticker/24hr", {"symbol": symbol.upper()})
        row = payload[0] if isinstance(payload, list) else payload
        return MarketSnapshot(symbol.upper(), float(row["lastPrice"]),
                              self._timestamp(row.get("closeTime")), "weex_spot_v3")

    def tickers(self) -> list[dict]:
        payload = self._get("/api/v3/market/ticker/24hr", {})
        return payload if isinstance(payload, list) else [payload]


class WeexFuturesMarketData(_WeexPublicClient):
    base_url = "https://api-contract.weex.com"

    def snapshot(self, symbol: str) -> MarketSnapshot:
        row = self._get("/capi/v3/market/symbolPrice",
                        {"symbol": symbol.upper(), "priceType": "MARK"})
        return MarketSnapshot(symbol.upper(), float(row["price"]),
                              self._timestamp(row.get("time")), "weex_futures_v3_mark")

    def supported_symbols(self) -> list[str]:
        payload = self._get("/capi/v3/market/apiTradingSymbols", {})
        return [str(symbol) for symbol in payload]

    def tickers(self) -> list[dict]:
        payload = self._get("/capi/v3/market/ticker/24hr", {})
        return payload if isinstance(payload, list) else [payload]


class TradingAgentsClient:
    def __init__(self, config: dict | None = None):
        self.config = config

    def analyze(self, symbol: str, as_of: str) -> TradeSignal:
        try:
            graph_class = _import_attribute((
                ("tradingagents.graph.trading_graph", "TradingAgentsGraph"),
                ("tradingagents.graph", "TradingAgentsGraph"),
                ("tradingagents", "TradingAgentsGraph"),
            ))
            config = self.config
            if config is None:
                default = _import_attribute((
                    ("tradingagents.default_config", "DEFAULT_CONFIG"),
                    ("tradingagents.config", "DEFAULT_CONFIG"),
                    ("tradingagents.config.default_config", "DEFAULT_CONFIG"),
                    ("tradingagents", "DEFAULT_CONFIG"),
                ))
                config = default.copy()
            graph = graph_class(debug=False, config=config)
            _, decision = graph.propagate(research_symbol(symbol), as_of)
            raw = str(decision).upper()
            side = Side.BUY if "BUY" in raw else Side.SELL if "SELL" in raw else Side.HOLD
            confidence = 0.75 if side is not Side.HOLD else 0.0
            return TradeSignal(symbol.upper(), side, confidence, str(decision), "TradingAgents")
        except Exception as exc:
            return TradeSignal(symbol.upper(), Side.HOLD, 0.0,
                               f"Analysis unavailable: {type(exc).__name__}: {exc}",
                               "safe_fallback")


def _import_attribute(candidates: tuple[tuple[str, str], ...]):
    errors = []
    for module_name, attribute in candidates:
        try:
            return getattr(importlib.import_module(module_name), attribute)
        except (ImportError, AttributeError) as exc:
            errors.append(f"{module_name}.{attribute}: {exc}")
    raise ImportError("No compatible TradingAgents package layout found (" + "; ".join(errors) + ")")


class PaperclipReporter:
    def __init__(self, base_url: str | None = None, api_key: str | None = None):
        self.base_url = (base_url or os.getenv("PAPERCLIP_API_URL", "")).rstrip("/")
        self.api_key = api_key or os.getenv("PAPERCLIP_API_KEY", "")

    def report(self, event: dict) -> None:
        """Send an event to an explicitly configured task-bridge endpoint."""
        endpoint = os.getenv("PAPERCLIP_TASK_BRIDGE_URL", "")
        if not endpoint:
            return
        body = json.dumps(event).encode()
        request = Request(endpoint, data=body, method="POST", headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"})
        with urlopen(request, timeout=10):
            pass

    @property
    def configured(self) -> bool:
        return bool(os.getenv("PAPERCLIP_TASK_BRIDGE_URL", "") and self.api_key)
