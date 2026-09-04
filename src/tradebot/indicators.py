"""Small, deterministic technical indicators with no dataframe dependency."""

from __future__ import annotations

from collections.abc import Sequence
from math import sqrt

from .models import Candle


def ema(values: Sequence[float], period: int) -> list[float | None]:
    if period <= 0:
        raise ValueError("period must be positive")
    result: list[float | None] = [None] * len(values)
    if len(values) < period:
        return result
    seed = sum(float(x) for x in values[:period]) / period
    result[period - 1] = seed
    multiplier = 2 / (period + 1)
    previous = seed
    for index in range(period, len(values)):
        previous = (float(values[index]) - previous) * multiplier + previous
        result[index] = previous
    return result


def rsi(values: Sequence[float], period: int = 14) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    if period <= 0 or len(values) <= period:
        return result
    changes = [float(values[i]) - float(values[i - 1]) for i in range(1, len(values))]
    gain = sum(max(x, 0) for x in changes[:period]) / period
    loss = sum(max(-x, 0) for x in changes[:period]) / period

    def value() -> float:
        if loss == 0:
            return 100.0 if gain > 0 else 50.0
        if gain == 0:
            return 0.0
        return 100 - 100 / (1 + gain / loss)

    result[period] = value()
    for index in range(period + 1, len(values)):
        change = changes[index - 1]
        gain = (gain * (period - 1) + max(change, 0)) / period
        loss = (loss * (period - 1) + max(-change, 0)) / period
        result[index] = value()
    return result


def macd(values: Sequence[float], fast: int = 12, slow: int = 26,
         signal: int = 9) -> dict[str, list[float | None]]:
    fast_values, slow_values = ema(values, fast), ema(values, slow)
    line = [None if a is None or b is None else a - b
            for a, b in zip(fast_values, slow_values)]
    valid = [x for x in line if x is not None]
    signal_valid = ema(valid, signal)
    signal_line: list[float | None] = [None] * (len(line) - len(valid)) + signal_valid
    histogram = [None if a is None or b is None else a - b
                 for a, b in zip(line, signal_line)]
    return {"line": line, "signal": signal_line, "histogram": histogram}


def bollinger_bands(values: Sequence[float], period: int = 20,
                    deviations: float = 2) -> dict[str, list[float | None]]:
    middle: list[float | None] = [None] * len(values)
    upper, lower = middle.copy(), middle.copy()
    for index in range(period - 1, len(values)):
        window = [float(x) for x in values[index - period + 1:index + 1]]
        average = sum(window) / period
        stddev = sqrt(sum((x - average) ** 2 for x in window) / period)
        middle[index], upper[index], lower[index] = (average, average + deviations * stddev,
                                                     average - deviations * stddev)
    return {"middle": middle, "upper": upper, "lower": lower}


def atr(candles: Sequence[Candle], period: int = 14) -> list[float | None]:
    if not candles:
        return []
    ranges = [candles[0].high - candles[0].low]
    for previous, current in zip(candles, candles[1:]):
        ranges.append(max(current.high - current.low, abs(current.high - previous.close),
                          abs(current.low - previous.close)))
    return ema(ranges, period)


def vwap(candles: Sequence[Candle]) -> list[float | None]:
    total_value = total_volume = 0.0
    output = []
    for bar in candles:
        if bar.volume is None or bar.volume < 0:
            output.append(None)
            continue
        total_volume += bar.volume
        total_value += ((bar.high + bar.low + bar.close) / 3) * bar.volume
        output.append(total_value / total_volume if total_volume else None)
    return output


def support_resistance(candles: Sequence[Candle], lookback: int = 50) -> dict[str, float | None]:
    recent = list(candles[-lookback:])
    if not recent:
        return {"support": None, "resistance": None}
    swing_lows = [recent[i].low for i in range(2, len(recent) - 2)
                  if recent[i].low <= min(x.low for x in recent[i-2:i+3])]
    swing_highs = [recent[i].high for i in range(2, len(recent) - 2)
                   if recent[i].high >= max(x.high for x in recent[i-2:i+3])]
    return {"support": swing_lows[-1] if swing_lows else min(x.low for x in recent),
            "resistance": swing_highs[-1] if swing_highs else max(x.high for x in recent)}


def last(values: Sequence[float | None]) -> float | None:
    return next((value for value in reversed(values) if value is not None), None)
