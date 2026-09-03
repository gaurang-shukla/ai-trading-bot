from unittest.mock import patch

from tradebot.models import MarketKind
from tradebot.overview import crypto_overview


def test_crypto_overview_ranks_real_ticker_fields():
    tickers = [
        {"symbol": "AAAUSDT", "lastPrice": "10", "priceChangePercent": "8",
         "highPrice": "11", "lowPrice": "9", "quoteVolume": "1000000"},
        {"symbol": "BBBUSDT", "lastPrice": "20", "priceChangePercent": "-4",
         "highPrice": "21", "lowPrice": "19", "quoteVolume": "500000"},
    ]
    with patch("tradebot.overview.WeexFuturesMarketData.tickers", return_value=tickers):
        result = crypto_overview(MarketKind.CRYPTO_FUTURES)
    assert result["gainers"][0]["symbol"] == "AAAUSDT"
    assert result["losers"][0]["symbol"] == "BBBUSDT"
    assert all(0 <= row["signal_score"] <= 100 for row in result["assets"])
