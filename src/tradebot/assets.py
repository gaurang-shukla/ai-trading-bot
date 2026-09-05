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
    instrument_type: str


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
    symbol: AssetMetadata(symbol, f"{symbol}=F", name, description, "Commodity futures")
    for symbol, (name, description) in _COMMODITY_NAMES.items()
}

_FOREX_PAIRS = ("EURUSD", "GBPUSD", "AUDUSD", "NZDUSD", "USDJPY", "USDCHF", "USDCAD")
FOREX = {
    symbol: AssetMetadata(
        symbol,
        f"{symbol}=X",
        f"{symbol[:3]}/{symbol[3:]}",
        "In forex, the first currency is the base currency and the second is the quote currency.",
        "Forex",
    )
    for symbol in _FOREX_PAIRS
}

_EQUITY_NAMES = {
    "HDFCBANK.NS": "HDFC Bank",
    "ICICIBANK.NS": "ICICI Bank",
    "RELIANCE.NS": "Reliance",
    "INFY.NS": "Infosys",
    "SBIN.NS": "SBI",
    "TCS.NS": "TCS",
    "AXISBANK.NS": "Axis Bank",
    "KOTAKBANK.NS": "Kotak Mahindra Bank",
    "INDUSINDBK.NS": "IndusInd Bank",
    "BANKBARODA.NS": "Bank of Baroda",
    "PNB.NS": "Punjab National Bank",
    "AUBANK.NS": "AU Small Finance Bank",
    "IDFCFIRSTB.NS": "IDFC First Bank",
    "FEDERALBNK.NS": "Federal Bank",
}


def asset_metadata(market: MarketKind, symbol: str) -> AssetMetadata:
    symbol = symbol.upper()
    if market is MarketKind.COMMODITIES and symbol in COMMODITIES:
        return COMMODITIES[symbol]
    if market is MarketKind.FOREX and symbol in FOREX:
        return FOREX[symbol]
    if market in (MarketKind.CRYPTO_FUTURES, MarketKind.CRYPTO_SPOT) and symbol.endswith("USDT"):
        kind = "Crypto futures" if market is MarketKind.CRYPTO_FUTURES else "Crypto spot"
        return AssetMetadata(symbol, symbol, f"{symbol[:-4]}/USDT", "", kind)
    if market is MarketKind.EQUITIES:
        display = _EQUITY_NAMES.get(symbol, symbol)
        kind = "Indian equity" if symbol.endswith(".NS") else "US equity"
        return AssetMetadata(symbol, symbol, display, "", kind)
    if market is MarketKind.INDIAN_INDICES and symbol in _EQUITY_NAMES:
        return AssetMetadata(symbol, symbol, _EQUITY_NAMES[symbol], "", "Indian equity")
    display = symbol.removesuffix(".NS") if symbol.endswith(".NS") else symbol
    kind = "Indian equity" if symbol.endswith(".NS") else "Indian index" if market is MarketKind.INDIAN_INDICES else market.value.replace("_", " ").title()
    return AssetMetadata(symbol, symbol, display, "", kind)


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
