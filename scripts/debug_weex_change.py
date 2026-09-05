#!/usr/bin/env python3
"""Read-only inspection of raw and normalized WEEX bulk ticker changes."""

from pprint import pformat

from tradebot.adapters import (WeexFuturesMarketData, WeexSpotMarketData,
                               normalize_weex_ticker_row, weex_change_details)
from tradebot.models import MarketKind
from tradebot.overview import _ticker_rows, _score_overview


def inspect(label, market, client):
    raw_rows = client.raw_tickers()
    normalized = [normalize_weex_ticker_row(row, label) for row in raw_rows]
    overview_rows = _ticker_rows(normalized)
    selected = {row.get("symbol") for row in sorted(
        overview_rows, key=lambda row: row["change"], reverse=True)[:5]}
    selected.update({"BULLAUSDT", "BNBUSDT", "BTCUSDT"})
    print(f"\n=== {label} ===")
    for raw, ticker in zip(raw_rows, normalized):
        if raw.get("symbol") not in selected:
            continue
        field, value, percent = weex_change_details(raw)
        print(f"symbol: {raw.get('symbol')}")
        print(f"raw row keys: {sorted(raw)}")
        print(f"raw change field selected: {field}")
        print(f"raw change value: {value!r}")
        print(f"normalized change percent: {percent!r}")
        print(f"final provider ticker object: {pformat(ticker)}")
    overview = _score_overview(market, overview_rows, {"WEEX"})
    print("final overview top gainers:")
    print(pformat(overview["gainers"]))


if __name__ == "__main__":
    inspect("crypto_futures", MarketKind.CRYPTO_FUTURES, WeexFuturesMarketData())
    inspect("crypto_spot", MarketKind.CRYPTO_SPOT, WeexSpotMarketData())
