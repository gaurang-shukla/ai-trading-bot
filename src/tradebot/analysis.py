"""Fast deterministic signals and caching for opt-in AI research."""

from __future__ import annotations

import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import asdict
from datetime import datetime, timezone
from math import log10
from typing import Callable

from .indicators import atr, bollinger_bands, ema, last, macd, rsi, support_resistance, vwap
from .models import Candle, MarketSnapshot, Side, TradeSignal

TIMEFRAMES = ("1m", "5m", "15m", "1h", "4h", "1d")

HOLD_EXPLANATION = (
    "HOLD means the app does not see a strong enough setup to open a new trade right now. "
    "Because this is HOLD, the app is not suggesting a new entry. Watch resistance for a "
    "possible bullish breakout and support for possible downside risk."
)


def ensure_risk_plan(result: dict) -> dict:
    """Attach one stable, explicit risk-plan contract to any analysis result."""
    signal = result.setdefault("signal", {})
    levels = result.get("key_levels") or {}
    side = str(getattr(signal.get("side"), "value", signal.get("side") or "HOLD")).upper()
    active = side in {"BUY", "SELL", "STRONG BUY", "STRONG SELL"}
    risk = result.get("risk") or {}
    quick_signal = (result.get("quick_signal") or {}).get("signal") or {}
    position_size = signal.get("position_size_pct")
    if position_size is None:
        position_size = risk.get("position_size_pct", quick_signal.get("position_size_pct"))
    risk_score = signal.get("risk_score")
    if risk_score is None:
        risk_score = risk.get("score", quick_signal.get("risk_score", 1.0))
    stop = signal.get("stop_loss") if active else None
    target = signal.get("take_profit") if active else None
    if active:
        stop = stop if stop is not None else levels.get("stop_loss")
        target = target if target is not None else levels.get("take_profit")

    # Provider/AI signals occasionally omit execution levels. An active setup must
    # still have a usable plan, so use nearby technical levels and a conservative
    # one-to-two fallback around the current price.
    price = result.get("live_price", (result.get("market") or {}).get("price"))
    bearish = "SELL" in side
    if active and price is not None:
        distance = max(abs(float(price)) * .01, 1e-8)
        if stop is None:
            candidate = levels.get("resistance" if bearish else "support")
            stop = candidate if candidate is not None and ((candidate > price) if bearish else (candidate < price)) else price + distance if bearish else price - distance
        if target is None:
            candidate = levels.get("support" if bearish else "resistance")
            target = candidate if candidate is not None and ((candidate < price) if bearish else (candidate > price)) else price - distance * 2 if bearish else price + distance * 2

    signal.update({"stop_loss": stop, "take_profit": target,
                   "position_size_pct": 0.0 if not active else (position_size or 0.0),
                   "risk_score": risk_score})
    explanation = (HOLD_EXPLANATION if not active else
                   "Stop loss is placed above the invalidation area. Take profit is placed near the next downside target."
                   if bearish else
                   "Stop loss is placed below the invalidation area. Take profit is placed near the next upside target.")
    result["risk_plan"] = {
        "action": side, "has_active_trade_setup": active,
        "stop_loss": stop, "take_profit": target,
        "position_size_pct": signal["position_size_pct"], "risk_score": risk_score,
        "invalidation_level": stop if active else None,
        "breakout_level": levels.get("resistance"),
        "breakdown_level": levels.get("support"),
        "explanation": explanation,
    }
    return result


class CandleCache:
    """Short-lived, thread-safe candle cache; failed requests are not cached."""

    def __init__(self, ttl_seconds: float | None = None):
        self.ttl_seconds = ttl_seconds or float(os.getenv("CANDLE_CACHE_TTL_SECONDS", "45"))
        self._values: dict[tuple[str, str], tuple[float, list[Candle]]] = {}
        self._lock = threading.Lock()

    def get_or_load(self, symbol: str, timeframe: str,
                    loader: Callable[[], list[Candle]]) -> list[Candle]:
        key, now = (symbol.upper(), timeframe), time.monotonic()
        with self._lock:
            entry = self._values.get(key)
            if entry and now - entry[0] < self.ttl_seconds:
                return entry[1]
        value = loader()
        if value:
            with self._lock:
                self._values[key] = (now, value)
        return value


def score_ema_trend(price: float, ema9: float | None, ema21: float | None,
                    ema50: float | None, ema200: float | None) -> float:
    available = [x for x in (ema9, ema21, ema50, ema200) if x is not None]
    if len(available) < 2:
        return 0.0
    comparisons = [(price, ema9), (ema9, ema21), (ema21, ema50), (ema50, ema200)]
    valid = [(a, b) for a, b in comparisons if a is not None and b is not None]
    return sum(1 if a > b else -1 if a < b else 0 for a, b in valid) / len(valid) * 3


def score_macd(line: float | None, signal: float | None, histogram: float | None) -> float:
    if line is None or signal is None:
        return 0.0
    score = 1.25 if line > signal else -1.25 if line < signal else 0.0
    if histogram is not None:
        score += .5 if histogram > 0 else -.5 if histogram < 0 else 0
    return score


class QuickSignalEngine:
    """Turn one live quote into a useful signal without invoking an LLM."""

    def analyze(self, market: MarketSnapshot, equity: float,
                candles: dict[str, list[Candle]] | None = None) -> dict:
        usable = {frame: bars for frame, bars in (candles or {}).items() if len(bars) >= 20}
        if usable:
            return self._technical_analysis(market, equity, usable)
        return self._live_price_fallback(market, equity)

    def _live_price_fallback(self, market: MarketSnapshot, equity: float) -> dict:
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
        return ensure_risk_plan({
            "mode": "quick", "market": asdict(market), "signal": asdict(signal),
            "risk": {"score": risk_score, "position_size_pct": position_pct,
                     "position_notional": equity * position_pct},
            "trend_summary": "Candle history unavailable; using 24-hour price momentum.",
            "momentum_summary": "Momentum is based on the live 24-hour change.",
            "volatility_summary": "Risk uses reported or estimated 24-hour volatility.",
            "key_levels": {"support": None, "resistance": None, "stop_loss": stop,
                           "take_profit": target}, "timeframe_breakdown": [],
            "plain_language_reason": f"{rationale} {HOLD_EXPLANATION}" if side is Side.HOLD else f"{rationale} " + ("Stop loss is placed above the invalidation area. Take profit is placed near the next downside target." if bearish else "Stop loss is placed below the invalidation area. Take profit is placed near the next upside target."), "fallback": True,
        })

    def _technical_analysis(self, market: MarketSnapshot, equity: float,
                            frames: dict[str, list[Candle]]) -> dict:
        breakdown, weighted_score, total_weight, atr_percentages = [], 0.0, 0.0, []
        weights = {"1m": .5, "5m": .75, "15m": 1, "1h": 1.5, "4h": 2, "1d": 2.5}
        supports, resistances = [], []
        for frame in TIMEFRAMES:
            bars = frames.get(frame)
            if not bars:
                continue
            closes = [bar.close for bar in bars]
            emas = {period: last(ema(closes, period)) for period in (9, 21, 50, 200)}
            rsi_value = last(rsi(closes))
            macd_values = macd(closes)
            macd_line, macd_signal = last(macd_values["line"]), last(macd_values["signal"])
            histogram = last(macd_values["histogram"])
            bands = bollinger_bands(closes)
            upper, middle, lower = (last(bands[key]) for key in ("upper", "middle", "lower"))
            atr_value, vwap_value = last(atr(bars)), last(vwap(bars))
            levels = support_resistance(bars)
            if levels["support"] is not None: supports.append(levels["support"])
            if levels["resistance"] is not None: resistances.append(levels["resistance"])
            ema_score = score_ema_trend(closes[-1], emas[9], emas[21], emas[50], emas[200])
            momentum_score = 0 if rsi_value is None else (1.25 if 52 <= rsi_value <= 70 else
                -1.25 if 30 <= rsi_value <= 48 else -.5 if rsi_value > 75 else .5 if rsi_value < 25 else 0)
            macd_score = score_macd(macd_line, macd_signal, histogram)
            band_score = 0 if None in (upper, middle, lower) else (
                -1 if closes[-1] > upper else 1 if closes[-1] < lower else .5 if closes[-1] > middle else -.5)
            volume_score = 0
            if vwap_value is not None:
                volume_score = .5 if closes[-1] > vwap_value else -.5
            frame_score = max(-10, min(10, ema_score + momentum_score + macd_score + band_score + volume_score))
            weight = weights[frame]
            weighted_score += frame_score * weight
            total_weight += weight
            if atr_value and closes[-1]: atr_percentages.append(atr_value / closes[-1] * 100)
            breakdown.append({"timeframe": frame,
                              "trend": "Bullish" if ema_score > .5 else "Bearish" if ema_score < -.5 else "Neutral",
                              "rsi": round(rsi_value, 2) if rsi_value is not None else None,
                              "macd": "Bullish" if macd_score > 0 else "Bearish" if macd_score < 0 else "Neutral",
                              "ema_bias": round(ema_score, 2), "score": round(frame_score, 2)})
        score = weighted_score / total_weight if total_weight else 0
        funding_adjustment = max(-1, min(1, -(market.funding_rate or 0) * 2000))
        score += funding_adjustment
        side = (Side.STRONG_BUY if score >= 4.5 else Side.BUY if score >= 1.25 else
                Side.STRONG_SELL if score <= -4.5 else Side.SELL if score <= -1.25 else Side.HOLD)
        volatility_pct = sum(atr_percentages) / len(atr_percentages) if atr_percentages else abs(market.volatility_24h or 2)
        risk_score = max(.08, min(1, .18 + volatility_pct / 12 + .08 * (6 - len(breakdown))))
        confidence = max(.5, min(.95, .54 + abs(score) / 18 + len(breakdown) * .025 - risk_score * .08))
        probability = .5 if side is Side.HOLD else confidence
        position_pct = 0 if side is Side.HOLD else min(.05, .05 * confidence * (1 - risk_score))
        bearish = side in (Side.SELL, Side.STRONG_SELL)
        distance = max(market.price * .0075, market.price * volatility_pct / 100 * 1.5)
        stop = None if side is Side.HOLD else market.price + distance if bearish else market.price - distance
        target = None if side is Side.HOLD else market.price - distance * 2 if bearish else market.price + distance * 2
        support = max((x for x in supports if x < market.price), default=min(supports, default=None))
        resistance = min((x for x in resistances if x > market.price), default=max(resistances, default=None))
        trend = f"{sum(row['trend'] == 'Bullish' for row in breakdown)} bullish, {sum(row['trend'] == 'Bearish' for row in breakdown)} bearish timeframes."
        momentum = "RSI and MACD confirmation are mixed." if abs(score) < 2 else f"Momentum confirms a {'bullish' if score > 0 else 'bearish'} bias."
        volatility = f"Average ATR is {volatility_pct:.2f}% of price; risk is {'elevated' if risk_score > .65 else 'controlled'}."
        risk_reason = (HOLD_EXPLANATION if side is Side.HOLD else
                       "Stop loss is placed above the invalidation area. Take profit is placed near the next downside target."
                       if bearish else
                       "Stop loss is placed below the invalidation area. Take profit is placed near the next upside target.")
        reason = (f"The {side.value} result combines {len(breakdown)} available timeframes. {trend} "
                  f"{momentum} {volatility} Funding is included when available. No AI model was called. "
                  f"{risk_reason}")
        signal = TradeSignal(market.symbol, side, confidence, reason, "multi_timeframe_ta_v1",
                             risk_score, stop, target, probability, position_pct)
        return ensure_risk_plan({"mode": "quick", "market": asdict(market), "signal": asdict(signal),
                "risk": {"score": risk_score, "position_size_pct": position_pct,
                         "position_notional": equity * position_pct},
                "trend_summary": trend, "momentum_summary": momentum,
                "volatility_summary": volatility,
                "key_levels": {"support": support, "resistance": resistance,
                               "stop_loss": stop, "take_profit": target},
                "timeframe_breakdown": breakdown, "plain_language_reason": reason,
                "fallback": False})


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


class DeepJobRegistry:
    """Process-local Deep AI jobs with de-duplication and completed-result caching."""

    ACTIVE = {"queued", "running"}
    TERMINAL = {"completed", "failed", "timed_out"}
    STEPS = (
        "Preparing market data", "Checking technical indicators",
        "Running TradingAgents research", "Building plain-language summary",
        "Finalizing decision",
    )

    def __init__(self, ttl_seconds: float | None = None):
        self.ttl_seconds = ttl_seconds or float(os.getenv("DEEP_ANALYSIS_CACHE_TTL_SECONDS", "1200"))
        self._jobs: dict[str, dict] = {}
        self._latest: dict[tuple[str, str], str] = {}
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="deep-job")

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def start(self, market: str, symbol: str, runner: Callable[[Callable[[int, str], None]], dict],
              fallback: Callable[[], dict], refresh: bool = False) -> dict:
        key = (market, symbol.upper())
        with self._lock:
            previous = self._jobs.get(self._latest.get(key, ""))
            if previous and previous["status"] in self.ACTIVE:
                return self._public(previous)
            if previous and previous["status"] == "completed" and not refresh:
                age = time.monotonic() - previous["completed_monotonic"]
                if age < self.ttl_seconds:
                    response = self._public(previous)
                    response["cached"] = True
                    return response
            now = self._now()
            job = {"job_id": uuid.uuid4().hex, "market": market, "symbol": symbol.upper(),
                   "status": "queued", "started_at": now, "updated_at": now,
                   "completed_at": None, "progress_step": 0,
                   "progress_message": self.STEPS[0], "result": None,
                   "fallback_result": None, "user_friendly_error": None,
                   "debug_error": None, "started_monotonic": time.monotonic()}
            self._jobs[job["job_id"]] = job
            self._latest[key] = job["job_id"]
            self._executor.submit(self._execute, job["job_id"], runner, fallback)
            return self._public(job)

    def _execute(self, job_id: str, runner: Callable, fallback: Callable[[], dict]) -> None:
        self._update(job_id, status="running", progress_step=1,
                     progress_message=self.STEPS[0])
        timeout = max(.01, float(os.getenv("SIGNAL_DEEP_AI_TIMEOUT_SECONDS", "90")))
        inner = ThreadPoolExecutor(max_workers=1, thread_name_prefix="deep-ai-work")
        future = inner.submit(runner, lambda step, message: self._progress(job_id, step, message))
        try:
            result = future.result(timeout=timeout)
            self._update(job_id, status="completed", progress_step=len(self.STEPS),
                         progress_message=self.STEPS[-1], result=result,
                         completed_at=self._now(), completed_monotonic=time.monotonic())
        except FutureTimeoutError as exc:
            message = "Deep AI reached its backend time limit. Quick Signal remains available."
            self._terminal_error(job_id, "timed_out", message, exc, fallback)
        except Exception as exc:
            message = "Deep AI could not complete. Quick Signal remains available."
            self._terminal_error(job_id, "failed", message, exc, fallback)
        finally:
            # Running provider calls cannot safely be killed; do not block this worker.
            inner.shutdown(wait=False, cancel_futures=True)

    def _terminal_error(self, job_id: str, status: str, message: str, exc: Exception,
                        fallback: Callable[[], dict]) -> None:
        try:
            fallback_result = fallback()
        except Exception:
            fallback_result = None
        fields = {"status": status, "completed_at": self._now(),
                  "fallback_result": fallback_result, "user_friendly_error": message}
        if _signal_debug_enabled():
            fields["debug_error"] = f"{type(exc).__name__}: {exc}"
        self._update(job_id, **fields)

    def _progress(self, job_id: str, step: int, message: str) -> None:
        self._update(job_id, progress_step=max(1, min(len(self.STEPS), step)),
                     progress_message=message)

    def _update(self, job_id: str, **fields) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                job.update(fields)
                job["updated_at"] = self._now()

    def get(self, job_id: str) -> dict | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return self._public(job) if job else None

    @staticmethod
    def _public(job: dict) -> dict:
        result = {key: value for key, value in job.items()
                  if not key.endswith("_monotonic") and (key != "debug_error" or value)}
        result["elapsed_seconds"] = round(time.monotonic() - job["started_monotonic"], 1)
        return result


def _signal_debug_enabled() -> bool:
    return os.getenv("SIGNAL_DEBUG", "").lower() in {"1", "true", "yes", "on"}


def normalize_deep_reasoning(deep: dict, quick: dict) -> dict:
    """Keep good agent prose, but expand terse decisions using deterministic evidence."""
    signal = deep.setdefault("signal", {})
    ensure_risk_plan(deep)
    rationale = str(signal.get("rationale") or deep.get("plain_language_reason") or "").strip()
    if len(rationale.split()) >= 40 and rationale.upper() not in {"HOLD", "BUY", "SELL"}:
        deep["plain_language_reason"] = rationale
        if deep["risk_plan"]["explanation"] not in rationale:
            rationale = f"{rationale}\n\nRisk plan: {deep['risk_plan']['explanation']}"
            signal["rationale"] = rationale
            deep["plain_language_reason"] = rationale
        return deep

    side = str(signal.get("side") or quick["signal"]["side"]).upper()
    rows = quick.get("timeframe_breakdown") or []
    bullish = sum(row.get("trend") == "Bullish" for row in rows)
    bearish = sum(row.get("trend") == "Bearish" for row in rows)
    neutral = len(rows) - bullish - bearish
    mixed = not rows or (bullish and bearish) or neutral
    parts = [f"Decision summary: The result is {side} because " +
             ("bullish and bearish signals are mixed." if mixed else
              f"the available timeframes have a {'bullish' if bullish else 'bearish'} bias.")]
    if rows:
        parts.append(f"Multi-timeframe confirmation: {bullish} timeframes are bullish, "
                     f"{bearish} are bearish, and {neutral} are neutral, so confirmation "
                     f"is {'not strong enough for a high-confidence trade' if mixed else 'directionally aligned' }.")
        rsi_values = [row.get("rsi") for row in rows if row.get("rsi") is not None]
        macd = {row.get("macd") for row in rows if row.get("macd")}
        ema_values = [row.get("ema_bias") for row in rows if row.get("ema_bias") is not None]
        parts.append("Indicators: " +
                     (f"RSI averages {sum(rsi_values) / len(rsi_values):.1f}; " if rsi_values else "RSI is unavailable; ") +
                     (f"MACD readings are {', '.join(sorted(macd)).lower()}; " if macd else "MACD is unavailable; ") +
                     (f"the average EMA bias is {sum(ema_values) / len(ema_values):+.2f}. " if ema_values else "EMA bias is unavailable. ") +
                     quick.get("volatility_summary", "ATR/risk data is unavailable."))
    else:
        parts.append("Multi-timeframe confirmation: Candle timeframes are unavailable, so the decision relies on live-price rules.")
        parts.append("Indicators: RSI, MACD, EMA bias, and ATR are unavailable for this run; the risk score uses live volatility instead.")
    price = quick.get("live_price", quick.get("market", {}).get("price"))
    change = quick.get("change_24h", quick.get("market", {}).get("change_24h"))
    volume = quick.get("volume", quick.get("market", {}).get("volume"))
    levels = quick.get("key_levels") or {}
    parts.append(f"Price context: Live price is {_display(price)}, 24h change is {_display(change, '%')}, "
                 f"and volume is {_display(volume)}. Support is {_display(levels.get('support'))} and "
                 f"resistance is {_display(levels.get('resistance'))}.")
    stop = signal.get("stop_loss", levels.get("stop_loss"))
    target = signal.get("take_profit", levels.get("take_profit"))
    size = signal.get("position_size_pct", quick.get("risk", {}).get("position_size_pct"))
    parts.append(f"Risk guidance: Risk score is {_display(signal.get('risk_score', quick['signal'].get('risk_score')))}, "
                 f"stop loss is {_display(stop)}, take profit is {_display(target)}, and position size is "
                 f"{_display(None if size is None else size * 100, '%')}.")
    meaning = (HOLD_EXPLANATION
               if side == "HOLD" else f"{side} is a research signal, not an instruction to place a live trade.")
    parts.append(f"Beginner-friendly meaning: {meaning}")
    parts.append("What to watch next: A breakout above resistance with improving momentum would support a stronger BUY setup. "
                 "A breakdown below support would increase SELL risk.")
    reason = "\n\n".join(parts)
    signal["rationale"] = reason
    deep["plain_language_reason"] = reason
    return ensure_risk_plan(deep)


def _display(value: object, suffix: str = "") -> str:
    if value is None:
        return "unavailable"
    if isinstance(value, (int, float)):
        return f"{value:,.2f}{suffix}"
    return f"{value}{suffix}"
