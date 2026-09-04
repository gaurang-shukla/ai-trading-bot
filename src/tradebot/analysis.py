"""Fast deterministic signals and caching for opt-in AI research."""

from __future__ import annotations

import os
import threading
import time
from dataclasses import asdict
from datetime import datetime, timezone
from math import log10
from typing import Callable

from .models import MarketSnapshot, Side, TradeSignal


class QuickSignalEngine:
    """Turn one live quote into a useful signal without invoking an LLM."""

    def analyze(self, market: MarketSnapshot, equity: float) -> dict:
        change = market.change_24h or 0.0
        volatility = market.volatility_24h if market.volatility_24h is not None else abs(change)
        funding = market.funding_rate
        momentum = max(-10.0, min(10.0, change))
        # Extreme positive funding makes longs crowded; negative funding makes shorts crowded.
        funding_adjustment = max(-2.0, min(2.0, -(funding or 0.0) * 100 * 20))
        score = momentum + funding_adjustment
        if score >= 5:
            side = Side.STRONG_BUY
        elif score >= 1:
            side = Side.BUY
        elif score <= -5:
            side = Side.STRONG_SELL
        elif score <= -1:
            side = Side.SELL
        else:
            side = Side.HOLD

        risk_score = max(0.05, min(1.0, 0.2 + volatility / 15 + (0.1 if funding is None else 0)))
        direction_strength = min(1.0, abs(score) / 8)
        liquidity_bonus = min(0.08, log10(max(1.0, market.volume or 1.0)) / 100)
        confidence = max(0.5, min(0.94, 0.52 + direction_strength * .34 + liquidity_bonus - risk_score * .08))
        probability = 0.5 if side is Side.HOLD else confidence
        position_pct = 0.0 if side is Side.HOLD else min(0.05, max(0.005, .05 * (1-risk_score) * confidence))
        price = market.price
        stop_distance = max(.0075, min(.08, volatility / 100 * .75))
        reward_distance = stop_distance * 2
        bearish = side in (Side.SELL, Side.STRONG_SELL)
        stop = price * (1 + stop_distance if bearish else 1 - stop_distance)
        target = price * (1 - reward_distance if bearish else 1 + reward_distance)
        if side is Side.HOLD:
            stop = target = None
        funding_text = "not available" if funding is None else f"{funding * 100:.4f}%"
        volume_text = "not reported" if market.volume is None else f"{market.volume:,.0f}"
        rationale = (
            f"The live price is {price:,.8g} with a {change:+.2f}% 24-hour move, "
            f"{volatility:.2f}% estimated volatility, volume of {volume_text}, and funding {funding_text}. "
            f"Momentum is {'positive' if score > .25 else 'negative' if score < -.25 else 'neutral'}. "
            f"The {side.value} signal uses deterministic momentum, volatility, liquidity, funding, and risk rules; no AI model was called."
        )
        signal = TradeSignal(market.symbol, side, confidence, rationale, "quick_rules_v1",
                             risk_score, stop, target, probability, position_pct)
        return {
            "mode": "quick", "market": asdict(market), "signal": asdict(signal),
            "risk": {"score": risk_score, "position_size_pct": position_pct,
                     "position_notional": equity * position_pct},
        }


class DeepAnalysisCache:
    """Thread-safe TTL cache keyed by market and symbol, independent of page loads."""

    def __init__(self, ttl_seconds: float | None = None):
        configured = float(os.getenv("DEEP_ANALYSIS_CACHE_TTL_SECONDS", "1200"))
        self.ttl_seconds = ttl_seconds if ttl_seconds is not None else max(600, min(1800, configured))
        self._values: dict[tuple[str, str], tuple[float, str, dict]] = {}
        self._lock = threading.Lock()

    def get_or_run(self, market: str, symbol: str, run: Callable[[], dict],
                   refresh: bool = False) -> dict:
        key = (market, symbol.upper())
        now = time.monotonic()
        with self._lock:
            cached = self._values.get(key)
            if not refresh and cached and now - cached[0] < self.ttl_seconds:
                return self._response(cached, True)
        result = run()
        created_at = datetime.now(timezone.utc).isoformat()
        entry = (time.monotonic(), created_at, result)
        # Do not turn transient AI failures into a 20-minute dead end.
        if result.get("ai_available", True):
            with self._lock:
                self._values[key] = entry
        return self._response(entry, False)

    @staticmethod
    def _response(entry: tuple[float, str, dict], cached: bool) -> dict:
        return {**entry[2], "mode": "deep", "cached": cached,
                "deep_analyzed_at": entry[1]}
