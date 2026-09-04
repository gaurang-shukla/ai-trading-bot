import importlib
import json
import logging
import os
import threading
import time
from datetime import date, datetime, timezone
from typing import Protocol
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .models import MarketSnapshot, Side, TradeSignal


_CRYPTO_QUOTES = ("USDT", "USDC", "USD")
logger = logging.getLogger(__name__)


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

    def __init__(self, base_url: str | None = None, asset_class: str = "equity"):
        self.base_url = (base_url or os.getenv("OPENBB_API_URL", "http://127.0.0.1:6900")).rstrip("/")
        self.asset_class = asset_class

    def snapshot(self, symbol: str) -> MarketSnapshot:
        url = f"{self.base_url}/api/v1/{self.asset_class}/price/quote?{urlencode({'symbol': symbol})}"
        with urlopen(url, timeout=20) as response:
            payload = json.load(response)
        results = payload if isinstance(payload, list) else payload.get("results", payload.get("data", []))
        row = results[0] if isinstance(results, list) else results
        price = row.get("last_price") or row.get("price") or row.get("close")
        if price is None:
            raise ValueError(f"OpenBB returned no price for {symbol}")
        previous = row.get("prev_close") or row.get("previous_close")
        change = row.get("percent_change") or row.get("change_percent")
        if change is None and previous:
            change = (float(price) - float(previous)) / float(previous) * 100
        volume = row.get("volume") or row.get("regular_market_volume")
        return MarketSnapshot(symbol.upper(), float(price), row.get("last_trade_timestamp", date.today().isoformat()),
                              "OpenBB", _optional_float(change), _optional_float(volume))


class YahooFinanceClient:
    """Yahoo chart adapter with no optional Python-package dependency."""

    def snapshot(self, symbol: str) -> MarketSnapshot:
        url = ("https://query1.finance.yahoo.com/v8/finance/chart/"
               f"{quote(symbol, safe='')}?interval=1d&range=5d")
        request = Request(url, headers={"User-Agent": "Mozilla/5.0 Signal/0.5"})
        with urlopen(request, timeout=10) as response:
            payload = json.load(response)
        result = payload.get("chart", {}).get("result") or []
        if not result:
            raise ValueError(f"Yahoo Finance returned no quote for {symbol}")
        chart = result[0]
        meta = chart.get("meta", {})
        quote_row = (chart.get("indicators", {}).get("quote") or [{}])[0]
        closes = [value for value in quote_row.get("close", []) if value is not None]
        price = meta.get("regularMarketPrice") or (closes[-1] if closes else None)
        previous = meta.get("chartPreviousClose") or meta.get("previousClose")
        if price is None:
            raise ValueError(f"Yahoo Finance returned no price for {symbol}")
        change = ((float(price) - float(previous)) / float(previous) * 100) if previous else None
        volumes = [value for value in quote_row.get("volume", []) if value is not None]
        timestamp = datetime.fromtimestamp(
            int(meta.get("regularMarketTime") or time.time()), timezone.utc).isoformat()
        return MarketSnapshot(symbol.upper(), float(price), timestamp, "Yahoo Finance", change,
                              float(volumes[-1]) if volumes else None)


class NormalizedMarketData:
    """Query a research provider with its symbol while retaining the WEEX identity."""

    def __init__(self, provider: MarketData):
        self.provider = provider

    def snapshot(self, symbol: str) -> MarketSnapshot:
        result = self.provider.snapshot(research_symbol(symbol))
        return MarketSnapshot(symbol.upper(), result.price, result.as_of, result.source,
                              result.change_24h, result.volume)


class FallbackMarketData:
    def __init__(self, *providers: MarketData):
        self.providers = providers

    def snapshot(self, symbol: str) -> MarketSnapshot:
        errors = []
        for provider in self.providers:
            try:
                result = provider.snapshot(symbol)
                logger.info("%s\nProvider: %s", symbol.upper(), result.source)
                return result
            except Exception as exc:
                reason = f"{type(provider).__name__}: {type(exc).__name__}: {exc}"
                logger.warning("%s provider failed: %s", symbol.upper(), reason)
                errors.append(reason)
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
            config = _bounded_tradingagents_config(config)
            graph = graph_class(debug=False, config=config)
            _, decision = graph.propagate(research_symbol(symbol), as_of)
            raw = str(decision).upper()
            side = Side.BUY if "BUY" in raw else Side.SELL if "SELL" in raw else Side.HOLD
            confidence = 0.75 if side is not Side.HOLD else 0.0
            return TradeSignal(symbol.upper(), side, confidence, str(decision), "TradingAgents")
        except Exception as exc:
            logger.warning("AI analysis failed for %s: %s: %s", symbol.upper(), type(exc).__name__, exc)
            return TradeSignal(symbol.upper(), Side.HOLD, 0.0,
                               "AI temporarily unavailable. Showing live market data only.",
                               "safe_fallback")


def _bounded_tradingagents_config(config: dict) -> dict:
    """Apply conservative output/debate limits without mutating package defaults."""
    bounded = config.copy()
    max_tokens = min(600, max(100, int(os.getenv("TRADINGAGENTS_MAX_TOKENS", "550"))))
    bounded["max_tokens"] = max_tokens
    bounded["max_debate_rounds"] = min(int(bounded.get("max_debate_rounds", 1)), 1)
    bounded["max_risk_discuss_rounds"] = min(int(bounded.get("max_risk_discuss_rounds", 1)), 1)
    for key in ("llm_kwargs", "model_kwargs"):
        values = dict(bounded.get(key) or {})
        values["max_tokens"] = max_tokens
        bounded[key] = values
    return bounded


class CachedSignalProvider:
    """Thread-safe TTL cache preventing duplicate expensive AI analyses."""

    def __init__(self, provider: SignalProvider, ttl_seconds: float | None = None):
        self.provider = provider
        self.ttl_seconds = ttl_seconds if ttl_seconds is not None else float(
            os.getenv("AI_ANALYSIS_CACHE_TTL_SECONDS", "600"))
        self._cache: dict[tuple[str, str], tuple[float, TradeSignal]] = {}
        self._lock = threading.Lock()

    def analyze(self, symbol: str, as_of: str) -> TradeSignal:
        key = (symbol.upper(), as_of)
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(key)
            if cached and now - cached[0] < self.ttl_seconds:
                logger.info("%s AI analysis cache hit", key[0])
                return cached[1]
        result = self.provider.analyze(*key)
        with self._lock:
            self._cache[key] = (now, result)
        return result


def _optional_float(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


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
