from __future__ import annotations

from statistics import mean

from .adapters import WeexFuturesMarketData, WeexSpotMarketData
from .models import MarketKind


SEED_UNIVERSES = {
    MarketKind.EQUITIES: ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "JPM", "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "INFY.NS"],
    MarketKind.FOREX: ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF", "NZDUSD"],
    MarketKind.COMMODITIES: ["GC", "SI", "CL", "NG", "HG", "ZC", "ZW"],
    MarketKind.OPTIONS: ["AAPL", "NVDA", "SPY", "QQQ", "TSLA", "MSFT"],
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


def crypto_overview(market: MarketKind) -> dict:
    client = WeexFuturesMarketData() if market is MarketKind.CRYPTO_FUTURES else WeexSpotMarketData()
    raw = client.tickers()
    rows = []
    for item in raw:
        try:
            symbol = str(item.get("symbol", "")).upper()
            price = float(item.get("lastPrice") or item.get("last") or 0)
            change = float(item.get("priceChangePercent") or 0)
            high = float(item.get("highPrice") or item.get("high_24h") or price)
            low = float(item.get("lowPrice") or item.get("low_24h") or price)
            volume = float(item.get("quoteVolume") or item.get("volume_24h") or 0)
            if not symbol or price <= 0:
                continue
            volatility = abs(high - low) / price * 100 if price else 0
            rows.append({"symbol": symbol, "price": price, "change": change,
                         "high": high, "low": low, "volume": volume,
                         "volatility": volatility})
        except (TypeError, ValueError):
            continue
    if not rows:
        raise ValueError("WEEX returned no usable ticker rows")

    ranked_volume = {row["symbol"]: rank for rank, row in enumerate(sorted(rows, key=lambda r: r["volume"]))}
    max_rank = max(1, len(rows) - 1)
    breadth = sum(1 for row in rows if row["change"] > 0) / len(rows) * 100
    avg_change = mean(max(-10, min(10, row["change"])) for row in rows)
    mood = _clamp(breadth * .65 + (50 + avg_change * 4) * .35)
    for row in rows:
        liquidity = ranked_volume[row["symbol"]] / max_rank * 100
        trend = _clamp(50 + row["change"] * 5)
        risk_quality = _clamp(100 - row["volatility"] * 5)
        row.update({
            "trend_score": trend,
            "sentiment_score": mood,
            "liquidity_score": round(liquidity),
            "risk_score": risk_quality,
            "signal_score": _clamp(trend * .42 + mood * .18 + liquidity * .22 + risk_quality * .18),
        })
    rows.sort(key=lambda r: r["volume"], reverse=True)
    return {
        "market": market.value,
        "source": "WEEX V3 public 24-hour tickers",
        "fear_greed": {"score": mood, "label": _mood_label(mood), "method": "Signal breadth model"},
        "summary": {"assets": len(rows), "advancers_pct": round(breadth, 1),
                    "total_quote_volume": sum(row["volume"] for row in rows)},
        "gainers": sorted(rows, key=lambda r: r["change"], reverse=True)[:5],
        "losers": sorted(rows, key=lambda r: r["change"])[:5],
        "assets": rows[:250],
    }


def market_overview(market: MarketKind) -> dict:
    if market in (MarketKind.CRYPTO_FUTURES, MarketKind.CRYPTO_SPOT):
        return crypto_overview(market)
    symbols = SEED_UNIVERSES.get(market, [])
    return {
        "market": market.value,
        "source": "OpenBB universe — live quotes load during analysis",
        "fear_greed": None,
        "summary": {"assets": len(symbols), "advancers_pct": None, "total_quote_volume": None},
        "gainers": [], "losers": [],
        "assets": [{"symbol": symbol, "price": None, "change": None, "signal_score": None}
                   for symbol in symbols],
    }
