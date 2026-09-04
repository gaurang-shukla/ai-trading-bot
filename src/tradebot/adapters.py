import importlib
import json
import logging
import os
import threading
import time
from importlib import metadata
from datetime import date, datetime, timezone
from typing import Protocol
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .models import Candle, MarketSnapshot, Side, TradeSignal
from .diagnostics import diagnostics


_CRYPTO_QUOTES = ("USDT", "USDC", "USD")
INDIAN_RESEARCH_SYMBOLS = {
    "BANKNIFTY": "^NSEBANK",
    "NIFTY50": "^NSEI",
    "FINNIFTY": "NIFTY_FIN_SERVICE.NS",
    "MIDCPNIFTY": "NIFTY_MID_SELECT.NS",
}
logger = logging.getLogger(__name__)


def research_symbol(symbol: str) -> str:
    """Translate an exchange crypto pair into the format used by research feeds."""
    value = symbol.upper().replace("/", "").replace(":", "")
    if value in INDIAN_RESEARCH_SYMBOLS:
        return INDIAN_RESEARCH_SYMBOLS[value]
    if "-" in value:
        return value
    for quote in _CRYPTO_QUOTES:
        if value.endswith(quote) and len(value) > len(quote):
            return f"{value[:-len(quote)]}-USD"
    return value


class MarketData(Protocol):
    def snapshot(self, symbol: str) -> MarketSnapshot: ...
    def candles(self, symbol: str, interval: str, limit: int = 250) -> list[Candle]: ...


class SignalProvider(Protocol):
    def analyze(self, symbol: str, as_of: str) -> TradeSignal: ...


class OpenBBClient:
    """OpenBB remains a separate service; this adapter consumes its public API."""

    def __init__(self, base_url: str | None = None, asset_class: str = "equity"):
        self.base_url = (base_url or os.getenv("OPENBB_API_URL", "http://127.0.0.1:6900")).rstrip("/")
        self.asset_class = asset_class

    def snapshot(self, symbol: str) -> MarketSnapshot:
        url = f"{self.base_url}/api/v1/{self.asset_class}/price/quote?{urlencode({'symbol': symbol})}"
        with urlopen(url, timeout=float(os.getenv("MARKET_DATA_TIMEOUT_SECONDS", "0.9"))) as response:
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

    def candles(self, symbol: str, interval: str, limit: int = 250) -> list[Candle]:
        # OpenBB deployments differ in provider coverage. This standard endpoint is
        # attempted first; FallbackMarketData transparently moves to Yahoo if absent.
        params = {"symbol": symbol, "interval": interval, "limit": str(limit)}
        url = f"{self.base_url}/api/v1/{self.asset_class}/price/historical?{urlencode(params)}"
        with urlopen(url, timeout=float(os.getenv("MARKET_DATA_TIMEOUT_SECONDS", "0.9"))) as response:
            payload = json.load(response)
        rows = payload if isinstance(payload, list) else payload.get("results", payload.get("data", []))
        return _rows_to_candles(rows)[-limit:]

    def option_chain(self, symbol: str, expiration: str | None = None) -> dict:
        params = {"symbol": symbol.upper()}
        if expiration:
            params["expiration"] = expiration
        url = f"{self.base_url}/api/v1/derivatives/options/chains?{urlencode(params)}"
        with urlopen(url, timeout=20) as response:
            payload = json.load(response)
        rows = payload if isinstance(payload, list) else payload.get("results", payload.get("data", []))
        if isinstance(rows, dict):
            rows = rows.get("results", [])
        normalized = [_normalize_option(row) for row in rows]
        normalized = [row for row in normalized if row.get("strike") is not None]
        expiries = sorted({row["expiration"] for row in normalized if row.get("expiration")})
        strikes = sorted({row["strike"] for row in normalized})
        calls = sum((row.get("open_interest") or 0) for row in normalized if row.get("option_type") == "call")
        puts = sum((row.get("open_interest") or 0) for row in normalized if row.get("option_type") == "put")
        return {"symbol": symbol.upper(), "source": "OpenBB", "expiries": expiries,
                "strikes": strikes, "put_call_ratio": puts / calls if calls else None,
                "max_pain": _max_pain(normalized), "contracts": normalized}


class YahooFinanceClient:
    """Yahoo chart adapter with no optional Python-package dependency."""

    def snapshot(self, symbol: str) -> MarketSnapshot:
        url = ("https://query1.finance.yahoo.com/v8/finance/chart/"
               f"{quote(symbol, safe='')}?interval=1d&range=5d")
        request = Request(url, headers={"User-Agent": "Mozilla/5.0 Signal/0.5"})
        with urlopen(request, timeout=float(os.getenv("MARKET_DATA_TIMEOUT_SECONDS", "0.9"))) as response:
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

    def candles(self, symbol: str, interval: str, limit: int = 250) -> list[Candle]:
        yahoo_interval, history = _yahoo_interval(interval)
        url = ("https://query1.finance.yahoo.com/v8/finance/chart/"
               f"{quote(symbol, safe='')}?{urlencode({'interval': yahoo_interval, 'range': history})}")
        request = Request(url, headers={"User-Agent": "Mozilla/5.0 Signal/0.5"})
        with urlopen(request, timeout=float(os.getenv("MARKET_DATA_TIMEOUT_SECONDS", "0.9"))) as response:
            payload = json.load(response)
        results = payload.get("chart", {}).get("result") or []
        if not results:
            raise ValueError(f"Yahoo Finance returned no candles for {symbol}")
        chart = results[0]
        quote_data = (chart.get("indicators", {}).get("quote") or [{}])[0]
        rows = []
        for index, timestamp in enumerate(chart.get("timestamp") or []):
            row = {"timestamp": timestamp}
            row.update({key: (values[index] if index < len(values) else None)
                        for key, values in quote_data.items()})
            rows.append(row)
        return _rows_to_candles(rows)[-limit:]


class NormalizedMarketData:
    """Query a research provider with its symbol while retaining the WEEX identity."""

    def __init__(self, provider: MarketData):
        self.provider = provider

    def snapshot(self, symbol: str) -> MarketSnapshot:
        result = self.provider.snapshot(research_symbol(symbol))
        return MarketSnapshot(symbol.upper(), result.price, result.as_of, result.source,
                              result.change_24h, result.volume, result.funding_rate,
                              result.volatility_24h)

    def candles(self, symbol: str, interval: str, limit: int = 250) -> list[Candle]:
        return self.provider.candles(research_symbol(symbol), interval, limit)


class FallbackMarketData:
    def __init__(self, *providers: MarketData):
        self.providers = providers

    def snapshot(self, symbol: str) -> MarketSnapshot:
        errors = []
        for provider in self.providers:
            try:
                result = provider.snapshot(symbol)
                logger.info("%s\nProvider: %s", symbol.upper(), result.source)
                source = ("weex" if result.source.lower().startswith("weex") else
                          "yahoo" if "yahoo" in result.source.lower() else
                          "openbb" if "openbb" in result.source.lower() else None)
                if source:
                    diagnostics.success(source)
                return result
            except Exception as exc:
                reason = f"{type(provider).__name__}: {type(exc).__name__}: {exc}"
                logger.warning("%s provider failed: %s", symbol.upper(), reason)
                errors.append(reason)
        raise RuntimeError("; ".join(errors) or "no market data providers configured")

    def candles(self, symbol: str, interval: str, limit: int = 250) -> list[Candle]:
        errors = []
        for provider in self.providers:
            try:
                bars = provider.candles(symbol, interval, limit)
                if bars:
                    return bars
                raise ValueError("empty candle response")
            except Exception as exc:
                errors.append(f"{type(provider).__name__}: {exc}")
        raise RuntimeError("; ".join(errors))


class _WeexPublicClient:
    base_url: str

    def _get(self, path: str, params: dict[str, str]) -> dict | list:
        url = f"{self.base_url}{path}?{urlencode(params)}"
        request = Request(url, headers={"User-Agent": "three-layer-tradebot/0.2"})
        with urlopen(request, timeout=float(os.getenv("MARKET_DATA_TIMEOUT_SECONDS", "0.9"))) as response:
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
                              self._timestamp(row.get("closeTime")), "weex_spot_v3",
                              _optional_float(row.get("priceChangePercent") or row.get("changeRate")),
                              _optional_float(row.get("quoteVolume") or row.get("volume")), None,
                              _ticker_volatility(row))

    def tickers(self) -> list[dict]:
        payload = self._get("/api/v3/market/ticker/24hr", {})
        return payload if isinstance(payload, list) else [payload]

    def candles(self, symbol: str, interval: str, limit: int = 250) -> list[Candle]:
        payload = self._get("/api/v3/market/klines", {"symbol": symbol.upper(),
                            "interval": interval, "limit": str(limit)})
        return _weex_candles(payload)


class WeexFuturesMarketData(_WeexPublicClient):
    base_url = "https://api-contract.weex.com"

    def snapshot(self, symbol: str) -> MarketSnapshot:
        payload = self._get("/capi/v3/market/ticker/24hr", {"symbol": symbol.upper()})
        row = payload[0] if isinstance(payload, list) else payload
        price = row.get("lastPrice") or row.get("last") or row.get("markPrice") or row.get("price")
        return MarketSnapshot(symbol.upper(), float(price),
                              self._timestamp(row.get("closeTime") or row.get("time")), "weex_futures_v3",
                              _optional_float(row.get("priceChangePercent") or row.get("changeRate")),
                              _optional_float(row.get("quoteVolume") or row.get("volume")),
                              _optional_float(row.get("fundingRate") or row.get("lastFundingRate")),
                              _ticker_volatility(row))

    def supported_symbols(self) -> list[str]:
        payload = self._get("/capi/v3/market/apiTradingSymbols", {})
        return [str(symbol) for symbol in payload]

    def tickers(self) -> list[dict]:
        payload = self._get("/capi/v3/market/ticker/24hr", {})
        return payload if isinstance(payload, list) else [payload]

    def candles(self, symbol: str, interval: str, limit: int = 250) -> list[Candle]:
        payload = self._get("/capi/v3/market/klines", {"symbol": symbol.upper(),
                            "interval": interval, "limit": str(limit)})
        return _weex_candles(payload)


class TradingAgentsClient:
    def __init__(self, config: dict | None = None):
        self.config = config

    def analyze(self, symbol: str, as_of: str) -> TradeSignal:
        try:
            logger.info("TradingAgents stage=model_initialization start")
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
            provider = str(config.get("llm_provider", "openai")).lower()
            key_name = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY",
                        "google": "GOOGLE_API_KEY", "groq": "GROQ_API_KEY",
                        "deepseek": "DEEPSEEK_API_KEY", "openrouter": "OPENROUTER_API_KEY"}.get(provider)
            key_loaded = bool(key_name and os.getenv(key_name))
            logger.info("TradingAgents stage=api_key_loading provider=%s env=%s loaded=%s",
                        provider, key_name, key_loaded)
            if not key_loaded:
                raise RuntimeError(f"{key_name or provider + ' API key'} is not configured")
            model = os.getenv("OPENAI_MODEL", "") or str(config.get("quick_think_llm") or "gpt-4o-mini")
            if provider == "openai":
                config["quick_think_llm"] = model
                config["deep_think_llm"] = os.getenv("OPENAI_DEEP_MODEL", "") or model
            logger.info("TradingAgents stage=model_initialization package=%s provider=%s model=%s graph=%s",
                        _package_version("tradingagents"), provider, model, graph_class.__module__)
            logger.info("TradingAgents stage=prompt_creation symbol=%s research_symbol=%s as_of=%s",
                        symbol.upper(), research_symbol(symbol), as_of)
            call_header = (f"Starting AI analysis...\nsymbol: {symbol.upper()}\n"
                           f"model: {model}\nprovider: {provider}")
            print(call_header, flush=True)
            logger.info(call_header)
            graph = graph_class(debug=False, config=config)
            logger.info("TradingAgents stage=llm_request start model=%s", model)
            _, decision = graph.propagate(research_symbol(symbol), as_of)
            logger.info("TradingAgents stage=llm_request complete response_type=%s", type(decision).__name__)
            raw = str(decision).upper()
            logger.info("TradingAgents stage=response_parsing characters=%d", len(raw))
            side = _parse_side(raw)
            confidence = _extract_metric(raw, "CONFIDENCE", .82 if "STRONG" in side.value else .72)
            risk_score = _extract_metric(raw, "RISK SCORE", 1 - confidence)
            probability = _extract_metric(raw, "PROBABILITY", confidence)
            size = _extract_metric(raw, "POSITION SIZE", max(0.01, (1-risk_score) * .05))
            logger.info("TradingAgents stage=signal_generation side=%s confidence=%.3f", side.value, confidence)
            completion = f"AI completed\n{side.value}\nconfidence {confidence:.3f}"
            print(completion, flush=True)
            logger.info(completion)
            diagnostics.success("tradingagents")
            diagnostics.success("openai" if provider == "openai" else provider)
            return TradeSignal(symbol.upper(), side, confidence, str(decision), model, risk_score,
                               _extract_number(raw, "STOP LOSS"), _extract_number(raw, "TAKE PROFIT"), probability, size)
        except Exception as exc:
            diagnostics.failure("tradingagents", exc)
            if "provider" in locals():
                diagnostics.failure("openai" if provider == "openai" else provider, exc)
            logger.exception("FULL PYTHON TRACEBACK\nTradingAgents pipeline failed for %s", symbol.upper())
            return TradeSignal(symbol.upper(), Side.HOLD, 0.0,
                               f"AI failed: {type(exc).__name__}: {exc}",
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


def _package_version(name: str) -> str:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return "not-installed"


def _parse_side(raw: str) -> Side:
    import re
    match = re.search(r"\b(STRONG\s+BUY|STRONG\s+SELL|BUY|SELL|HOLD)\b", raw)
    return Side(match.group(1)) if match else Side.HOLD


def _extract_metric(raw: str, label: str, default: float) -> float:
    import re
    match = re.search(rf"{label}\s*[:=-]?\s*(\d+(?:\.\d+)?)\s*%?", raw)
    if not match:
        return default
    value = float(match.group(1))
    return min(1.0, max(0.0, value / 100 if value > 1 else value))

def _extract_number(raw: str, label: str) -> float | None:
    import re
    match = re.search(rf"{label}\s*[:=-]?\s*\$?([\d,]+(?:\.\d+)?)", raw)
    return float(match.group(1).replace(",", "")) if match else None

def _normalize_option(row: dict) -> dict:
    def first(*keys):
        return next((row[k] for k in keys if row.get(k) is not None), None)
    return {"expiration": first("expiration", "expiration_date"),
            "strike": _optional_float(first("strike", "strike_price")),
            "option_type": str(first("option_type", "type") or "").lower(),
            "open_interest": _optional_float(first("open_interest", "openInterest")),
            "iv": _optional_float(first("implied_volatility", "iv")),
            "delta": _optional_float(first("delta")), "gamma": _optional_float(first("gamma")),
            "theta": _optional_float(first("theta")), "vega": _optional_float(first("vega")),
            "last_price": _optional_float(first("last_price", "last"))}


def _max_pain(rows: list[dict]) -> float | None:
    strikes = {row["strike"] for row in rows if row.get("strike") is not None}
    if not strikes:
        return None
    def payout(at):
        return sum((max(0, at-r["strike"]) if r.get("option_type") == "call" else
                    max(0, r["strike"]-at)) * (r.get("open_interest") or 0) for r in rows)
    return min(strikes, key=payout)


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


def _rows_to_candles(rows: list) -> list[Candle]:
    bars = []
    for row in rows or []:
        try:
            if not isinstance(row, dict):
                continue
            values = [row.get(key) for key in ("open", "high", "low", "close")]
            if any(value is None for value in values):
                continue
            bars.append(Candle(row.get("timestamp") or row.get("date") or row.get("datetime") or "",
                               *(float(value) for value in values), _optional_float(row.get("volume"))))
        except (TypeError, ValueError):
            continue
    return bars


def _weex_candles(payload: dict | list) -> list[Candle]:
    rows = payload.get("data", payload.get("result", [])) if isinstance(payload, dict) else payload
    bars = []
    for row in rows or []:
        try:
            if isinstance(row, dict):
                bars.extend(_rows_to_candles([row]))
            elif len(row) >= 5:
                bars.append(Candle(row[0], float(row[1]), float(row[2]), float(row[3]),
                                   float(row[4]), _optional_float(row[5]) if len(row) > 5 else None))
        except (TypeError, ValueError):
            continue
    # APIs commonly return newest first; indicators require chronological input.
    return sorted(bars, key=lambda bar: int(bar.timestamp) if str(bar.timestamp).isdigit() else str(bar.timestamp))


def _yahoo_interval(interval: str) -> tuple[str, str]:
    mapping = {"1m": ("1m", "7d"), "5m": ("5m", "60d"), "15m": ("15m", "60d"),
               "1h": ("60m", "1y"), "4h": ("1h", "1y"), "1d": ("1d", "2y")}
    return mapping.get(interval, ("1d", "2y"))


def _ticker_volatility(row: dict) -> float | None:
    high = _optional_float(row.get("highPrice") or row.get("high_24h"))
    low = _optional_float(row.get("lowPrice") or row.get("low_24h"))
    price = _optional_float(row.get("lastPrice") or row.get("last") or row.get("price"))
    return (high - low) / price * 100 if high is not None and low is not None and price else None


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
        try:
            with urlopen(request, timeout=10):
                pass
            diagnostics.success("paperclip")
        except Exception as exc:
            diagnostics.failure("paperclip", exc)
            raise

    @property
    def configured(self) -> bool:
        return bool(os.getenv("PAPERCLIP_TASK_BRIDGE_URL", "") and self.api_key)

    @property
    def enabled(self) -> bool:
        return os.getenv("PAPERCLIP_ENABLED", "").lower() in {"1", "true", "yes"} or self.configured
