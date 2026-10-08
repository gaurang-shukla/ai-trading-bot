"""Complete, lightweight WEEX instrument discovery and genuine-symbol search."""

from __future__ import annotations

from datetime import datetime, timezone
import math
import os
import re
import threading
import time
from typing import Callable

from .adapters import WeexFuturesMarketData, WeexSpotMarketData, normalize_weex_24h_change
from .models import MarketKind

ACTIVE = {"", "active", "online", "trading", "normal", "enabled", "1", "true"}
INACTIVE = {"suspended", "suspend", "delisted", "offline", "disabled", "closed", "0", "false"}
QUOTES = ("USDT", "USDC", "USD", "BTC", "ETH")


def canonical_symbol(value: object) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def split_symbol(symbol: str, row: dict) -> tuple[str, str]:
    base = canonical_symbol(row.get("baseAsset") or row.get("baseCoin") or row.get("baseCurrency"))
    quote = canonical_symbol(row.get("quoteAsset") or row.get("quoteCoin") or row.get("quoteCurrency"))
    if base and quote and symbol == base + quote: return base, quote
    for candidate in QUOTES:
        if symbol.endswith(candidate) and len(symbol) > len(candidate): return symbol[:-len(candidate)], candidate
    return base or symbol, quote


class WeexUniverseService:
    """Collects the full provider universe before filtering or deduplicating it."""

    def __init__(self, loader: Callable[[MarketKind, int, str | None], object] | None = None,
                 ttl: float = 60, max_pages: int = 100, clock=time.time):
        self.loader = loader or self._default_loader
        self.ttl, self.max_pages, self.clock = ttl, max(1, max_pages), clock
        self._cache: dict[MarketKind, tuple[float, dict]] = {}
        self._good: dict[MarketKind, dict] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _default_loader(market: MarketKind, page: int, cursor: str | None):
        # WEEX bulk ticker routes currently return the whole list in one batch.
        # The collector nevertheless understands cursor/page envelopes so endpoint
        # pagination can be enabled without changing normalization or search.
        if page > 1: return {"data": [], "hasMore": False}
        client = WeexFuturesMarketData() if market is MarketKind.CRYPTO_FUTURES else WeexSpotMarketData()
        return {"data": client.raw_tickers(), "hasMore": False}

    @staticmethod
    def _page(payload: object) -> tuple[list[dict], str | None, bool]:
        if isinstance(payload, list): return [r for r in payload if isinstance(r, dict)], None, False
        if not isinstance(payload, dict): return [], None, False
        data = payload.get("data", payload.get("result", payload.get("list", [])))
        if isinstance(data, dict):
            rows = data.get("list") or data.get("rows") or data.get("items") or []
            cursor = data.get("nextCursor") or data.get("next_cursor")
            more = bool(data.get("hasMore") or data.get("has_more") or cursor)
        else:
            rows, cursor = data, payload.get("nextCursor") or payload.get("next_cursor")
            explicit_more = payload.get("hasMore", payload.get("has_more"))
            more = bool(cursor) if explicit_more is None else bool(explicit_more)
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else [], str(cursor) if cursor else None, more

    def universe(self, market: MarketKind, refresh: bool = False) -> dict:
        if market not in {MarketKind.CRYPTO_SPOT, MarketKind.CRYPTO_FUTURES}:
            raise ValueError("WEEX universe is available only for crypto spot and futures")
        with self._lock:
            cached = self._cache.get(market)
            if cached and not refresh and self.clock() - cached[0] < self.ttl:
                result = dict(cached[1]); result["cache_age_seconds"] = round(self.clock() - cached[0], 3); return result
            try:
                result = self._collect(market)
                self._cache[market] = (self.clock(), result); self._good[market] = result
                return dict(result)
            except Exception:
                if market in self._good:
                    result = dict(self._good[market]); result.update(stale=True, provider_status="stale",
                        warning="Showing the last known genuine WEEX universe.")
                    return result
                raise

    def _collect(self, market: MarketKind) -> dict:
        raw, pages, cursor, fingerprints = [], 0, None, set()
        for page in range(1, self.max_pages + 1):
            payload = self.loader(market, page, cursor)
            rows, next_cursor, more = self._page(payload)
            fingerprint = tuple(canonical_symbol(r.get("symbol") or r.get("contractCode")) for r in rows)
            if fingerprint and fingerprint in fingerprints: break
            if fingerprint: fingerprints.add(fingerprint)
            pages += 1; raw.extend(rows)
            if not more or not rows: break
            if next_cursor and next_cursor == cursor: break
            cursor = next_cursor
        instruments, seen, invalid, duplicates = [], set(), 0, 0
        for row in raw:
            provider_symbol = str(row.get("symbol") or row.get("contractCode") or row.get("symbolName") or "")
            symbol = canonical_symbol(provider_symbol)
            status = str(row.get("status") or row.get("state") or row.get("tradeStatus") or "").lower()
            if not symbol or status in INACTIVE or (status not in ACTIVE and status): invalid += 1; continue
            if symbol in seen: duplicates += 1; continue
            base, quote = split_symbol(symbol, row)
            if not base or not quote: invalid += 1; continue
            seen.add(symbol)
            price = row.get("lastPrice") or row.get("last") or row.get("markPrice") or row.get("price")
            high, low, last = _number(row.get("highPrice")), _number(row.get("lowPrice")), _number(price)
            volatility = (high - low) / last * 100 if high is not None and low is not None and last and last > 0 and high >= low else None
            instruments.append({"symbol": symbol, "provider_symbol": provider_symbol,
                "display_symbol": f"{base}/{quote}", "base_asset": base, "quote_asset": quote,
                "status": "trading", "price": _number(price), "mark_price": _number(row.get("markPrice")),
                "change": normalize_weex_24h_change(row),
                "volume": _number(row.get("quoteVolume") or row.get("turnover") or row.get("volume")),
                "funding_rate": _number(row.get("fundingRate") if row.get("fundingRate") is not None else row.get("lastFundingRate")),
                "order_book_imbalance": _number(row.get("orderBookImbalance")),
                "open_interest": _number(row.get("openInterest")),
                "open_interest_change": _number(row.get("openInterestChange")),
                "volatility_24h": volatility,
                "available_on_weex": True, "ranked": False})
        now = datetime.now(timezone.utc).isoformat()
        return {"market": market.value, "source": "WEEX", "instruments": instruments,
            "stale": False, "provider_status": "connected", "last_refreshed": now,
            "cache_age_seconds": 0, "diagnostics": {"raw_records_retrieved": len(raw),
                "pages_retrieved": pages, "active_contracts_retained": len(instruments),
                "duplicates_removed": duplicates, "invalid_or_suspended_removed": invalid}}

    def search(self, market: MarketKind, query: str, refresh: bool = False, limit: int = 50) -> dict:
        universe = self.universe(market, refresh)
        normalized = canonical_symbol(query)
        rows = universe["instruments"]
        exact_base = [r for r in rows if r["base_asset"] == normalized]
        exact_symbol = [r for r in rows if r["symbol"] == normalized]
        if not exact_symbol and exact_base:
            # A bare base resolves only where exactly one genuine active market exists.
            exact_symbol = exact_base if len(exact_base) == 1 else []
        def rank(row):
            fields = (row["symbol"], canonical_symbol(row["display_symbol"]), row["base_asset"])
            if row in exact_symbol: return 0
            if any(value.startswith(normalized) for value in fields): return 1
            return 2
        matches = [r for r in rows if normalized and any(normalized in value for value in
                   (r["symbol"], canonical_symbol(r["display_symbol"]), r["base_asset"]))]
        matches.sort(key=lambda row: (rank(row), row["symbol"]))
        return {**{k: v for k, v in universe.items() if k != "instruments"}, "query": query,
                "normalized_query": normalized, "matches": matches[:max(1, min(limit, 100))],
                "found": bool(matches)}


def _number(value):
    try:
        number = float(value) if value is not None and value != "" else None
        return number if number is not None and math.isfinite(number) else None
    except (TypeError, ValueError): return None


weex_universes = WeexUniverseService(ttl=float(os.getenv("WEEX_UNIVERSE_CACHE_SECONDS", "60")))
