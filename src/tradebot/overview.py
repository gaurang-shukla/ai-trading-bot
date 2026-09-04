from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from statistics import mean

from .adapters import OpenBBClient, WeexFuturesMarketData, WeexSpotMarketData, YahooFinanceClient
from .models import MarketKind, MarketSnapshot

logger = logging.getLogger(__name__)

SEED_UNIVERSES = {
    MarketKind.CRYPTO_FUTURES: ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT"],
    MarketKind.CRYPTO_SPOT: ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT"],
    MarketKind.EQUITIES: ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "JPM", "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "INFY.NS"],
    MarketKind.FOREX: ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF", "NZDUSD"],
    MarketKind.COMMODITIES: ["GC", "SI", "CL", "NG", "HG", "ZC", "ZW"],
    MarketKind.OPTIONS: ["AAPL", "NVDA", "SPY", "QQQ", "TSLA", "MSFT"],
}

YAHOO_SYMBOLS = {
    **{symbol: f"{symbol}=X" for symbol in SEED_UNIVERSES[MarketKind.FOREX]},
    **{symbol: f"{symbol}=F" for symbol in SEED_UNIVERSES[MarketKind.COMMODITIES]},
}


def _clamp(value: float) -> int:
    return round(max(0, min(100, value)))


def _mood_label(score: int) -> str:
    if score < 25:
        return "Extreme fear"
    if score < 45:
        return "Fear"
    if score < 56:
        return "Neutral"
    if score < 76:
        return "Greed"
    return "Extreme greed"


def _ticker_rows(raw: list[dict]) -> list[dict]:
    rows = []
    for item in raw:
        try:
            symbol = str(item.get("symbol", "")).upper()
            price = float(item.get("lastPrice") or item.get("last") or 0)
            change = float(item.get("priceChangePercent") or item.get("changeRate") or 0)
            high = float(item.get("highPrice") or item.get("high_24h") or price)
            low = float(item.get("lowPrice") or item.get("low_24h") or price)
            volume = float(item.get("quoteVolume") or item.get("quoteVolume_24h") or item.get("volume_24h") or 0)
            if symbol and price > 0:
                rows.append({"symbol": symbol, "price": price, "change": change,
                             "high": high, "low": low, "volume": volume,
                             "volatility": abs(high - low) / price * 100})
        except (TypeError, ValueError):
            continue
    return rows


def _score_overview(market: MarketKind, rows: list[dict], sources: set[str]) -> dict:
    if not rows:
        raise RuntimeError(f"No provider returned usable {market.value} quotes")
    ranked_volume = {row["symbol"]: rank for rank, row in enumerate(sorted(rows, key=lambda row: row.get("volume") or 0))}
    max_rank = max(1, len(rows) - 1)
    breadth = sum(1 for row in rows if (row.get("change") or 0) > 0) / len(rows) * 100
    avg_change = mean(max(-10, min(10, row.get("change") or 0)) for row in rows)
    mood = _clamp(breadth * .65 + (50 + avg_change * 4) * .35)
    for row in rows:
        liquidity = ranked_volume[row["symbol"]] / max_rank * 100
        trend = _clamp(50 + (row.get("change") or 0) * 5)
        risk_quality = _clamp(100 - (row.get("volatility") or abs(row.get("change") or 0)) * 5)
        row.update({
            "trend_score": trend, "sentiment_score": mood,
            "liquidity_score": round(liquidity), "risk_score": risk_quality,
            "signal_score": _clamp(trend * .42 + mood * .18 + liquidity * .22 + risk_quality * .18),
        })
    rows.sort(key=lambda row: row.get("volume") or 0, reverse=True)
    source = " → ".join(sorted(sources, key=lambda value: ("OpenBB" not in value and "WEEX" not in value, value)))
    return {
        "market": market.value, "source": source,
        "fear_greed": {"score": mood, "label": _mood_label(mood), "method": "Signal breadth model"},
        "summary": {"assets": len(rows), "advancers_pct": round(breadth, 1),
                    "total_quote_volume": sum(row.get("volume") or 0 for row in rows)},
        "gainers": sorted(rows, key=lambda row: row.get("change") or 0, reverse=True)[:5],
        "losers": sorted(rows, key=lambda row: row.get("change") or 0)[:5],
        "assets": rows[:250],
    }


def _snapshot_row(symbol: str, snapshot: MarketSnapshot) -> dict:
    change = snapshot.change_24h
    return {"symbol": symbol, "price": snapshot.price, "change": change,
            "volume": snapshot.volume, "volatility": abs(change or 0)}


class MarketOverviewService:
    """Build consistent dashboards while preserving market-specific provider priority."""

    def _providers(self, market: MarketKind, symbol: str):
        yahoo_symbol = YAHOO_SYMBOLS.get(symbol, symbol)
        if market is MarketKind.OPTIONS:
            return [(symbol, OpenBBClient(asset_class="equity"))]
        asset_class = {MarketKind.EQUITIES: "equity", MarketKind.FOREX: "currency",
                       MarketKind.COMMODITIES: "commodity"}.get(market, "equity")
        return [(symbol, OpenBBClient(asset_class=asset_class)),
                (yahoo_symbol, YahooFinanceClient())]

    def _load_symbol(self, market: MarketKind, symbol: str) -> tuple[dict, str]:
        errors = []
        fallback = None
        providers = self._providers(market, symbol)
        for index, (provider_symbol, provider) in enumerate(providers):
            try:
                snapshot = provider.snapshot(provider_symbol)
                normalized = MarketSnapshot(symbol, snapshot.price, snapshot.as_of, snapshot.source,
                                            snapshot.change_24h, snapshot.volume)
                fallback = normalized
                # Incomplete primary quotes are not enough for a populated dashboard.
                if normalized.change_24h is None and index < len(providers) - 1:
                    raise ValueError("quote did not include daily change")
                logger.info("%s\nProvider: %s", symbol, normalized.source)
                return _snapshot_row(symbol, normalized), normalized.source
            except Exception as exc:
                reason = f"{type(exc).__name__}: {exc}"
                logger.warning("%s %s failed: %s", symbol, type(provider).__name__, reason)
                errors.append(reason)
        if fallback:
            return _snapshot_row(symbol, fallback), fallback.source
        raise RuntimeError("; ".join(errors))

    def build(self, market: MarketKind) -> dict:
        if market in (MarketKind.CRYPTO_FUTURES, MarketKind.CRYPTO_SPOT):
            client = WeexFuturesMarketData() if market is MarketKind.CRYPTO_FUTURES else WeexSpotMarketData()
            try:
                rows = _ticker_rows(client.tickers())
                if not rows:
                    raise ValueError("no usable ticker rows")
                logger.info("%s overview\nProvider: WEEX", market.value)
                return _score_overview(market, rows, {"WEEX"})
            except Exception as exc:
                logger.warning("%s WEEX overview failed: %s: %s; fallback: Yahoo Finance",
                               market.value, type(exc).__name__, exc)
                # Yahoo is deliberately only used when the preferred bulk feed fails.
                self._providers = lambda _market, symbol: [
                    (symbol.replace("USDT", "-USD"), YahooFinanceClient()),
                    (symbol.replace("USDT", "-USD"), OpenBBClient(asset_class="crypto")),
                ]

        rows, sources = [], set()
        symbols = SEED_UNIVERSES.get(market, [])
        with ThreadPoolExecutor(max_workers=min(8, len(symbols) or 1)) as executor:
            futures = {executor.submit(self._load_symbol, market, symbol): symbol for symbol in symbols}
            for future in as_completed(futures):
                try:
                    row, source = future.result()
                    rows.append(row)
                    sources.add(source)
                except Exception as exc:
                    logger.warning("%s exhausted all providers: %s", futures[future], exc)
        if rows:
            return _score_overview(market, rows, sources)
        # Keep the API schema stable if every external network is unavailable.
        # Values remain explicitly unavailable rather than being fabricated.
        return {
            "market": market.value, "source": "Live providers temporarily unavailable",
            "fear_greed": None,
            "summary": {"assets": len(symbols), "advancers_pct": None,
                        "total_quote_volume": None},
            "gainers": [], "losers": [],
            "assets": [{"symbol": symbol, "price": None, "change": None,
                        "volume": None, "signal_score": None} for symbol in symbols],
            "warning": "Live market data is temporarily unavailable.",
        }


class CachedMarketOverview:
    def __init__(self, ttl_seconds: float | None = None):
        self.ttl_seconds = ttl_seconds if ttl_seconds is not None else float(os.getenv("MARKET_CACHE_TTL_SECONDS", "60"))
        self._values: dict[MarketKind, tuple[float, dict]] = {}
        self._lock = threading.Lock()

    def get(self, market: MarketKind) -> dict:
        now = time.monotonic()
        with self._lock:
            cached = self._values.get(market)
            if cached and now - cached[0] < self.ttl_seconds:
                return cached[1]
        result = MarketOverviewService().build(market)
        with self._lock:
            self._values[market] = (now, result)
        return result


_overviews = CachedMarketOverview()


def crypto_overview(market: MarketKind) -> dict:
    return MarketOverviewService().build(market)


def market_overview(market: MarketKind) -> dict:
    return _overviews.get(market)
