"""Canonical user-facing asset metadata and provider symbol translation."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace

from .models import MarketKind, MarketSnapshot


@dataclass(frozen=True)
class AssetMetadata:
    symbol: str
    provider_symbol: str
    display_name: str
    description: str


_COMMODITY_NAMES = {
    "CL": ("Crude Oil", "Crude Oil futures track benchmark oil prices."),
    "ZC": ("Corn", "Corn futures track corn prices."),
    "GC": ("Gold", "Gold futures track gold market prices."),
    "NG": ("Natural Gas", "Natural Gas futures track natural gas prices."),
    "ZW": ("Wheat", "Wheat futures track wheat prices."),
    "SI": ("Silver", "Silver futures track silver prices."),
    "HG": ("Copper", "Copper futures track copper prices."),
}

COMMODITIES = {
    symbol: AssetMetadata(symbol, f"{symbol}=F", f"{symbol} ({name})", description)
    for symbol, (name, description) in _COMMODITY_NAMES.items()
}


def asset_metadata(market: MarketKind, symbol: str) -> AssetMetadata:
    symbol = symbol.upper()
    if market is MarketKind.COMMODITIES and symbol in COMMODITIES:
        return COMMODITIES[symbol]
    display = symbol.removesuffix(".NS") if symbol.endswith(".NS") else symbol
    return AssetMetadata(symbol, symbol, display, "")


def public_metadata(market: MarketKind, symbol: str) -> dict:
    return asdict(asset_metadata(market, symbol))


class ProviderSymbolMarketData:
    """Translate route symbols at the data-provider boundary only."""

    def __init__(self, provider, market: MarketKind):
        self.provider = provider
        self.market = market

    def snapshot(self, symbol: str) -> MarketSnapshot:
        metadata = asset_metadata(self.market, symbol)
        snapshot = self.provider.snapshot(metadata.provider_symbol)
        return replace(snapshot, symbol=metadata.symbol)

    def candles(self, symbol: str, timeframe: str, limit: int):
        metadata = asset_metadata(self.market, symbol)
        return self.provider.candles(metadata.provider_symbol, timeframe, limit)
