from collections.abc import Callable

from .adapters import (FallbackMarketData, MarketData, NormalizedMarketData,
                       OpenBBClient, WeexFuturesMarketData, WeexSpotMarketData,
                       YahooFinanceClient)
from .models import MarketKind, MarketSelection


class VenueRegistry:
    """Maps a user selection to a market-data adapter without strategy coupling."""

    def __init__(self):
        self._data: dict[tuple[MarketKind, str], Callable[[], MarketData]] = {}

    def register(self, market: MarketKind, venue: str, factory: Callable[[], MarketData]) -> None:
        self._data[(market, venue.lower())] = factory

    def market_data(self, selection: MarketSelection) -> MarketData:
        key = (selection.market, selection.venue.lower())
        if key not in self._data:
            available = ", ".join(f"{m.value}:{v}" for m, v in sorted(self._data, key=lambda x: (x[0].value, x[1])))
            raise ValueError(f"unsupported market/venue {selection.market.value}:{selection.venue}; available: {available}")
        return self._data[key]()

    def choices(self) -> list[dict[str, str]]:
        return [{"market": market.value, "venue": venue}
                for market, venue in sorted(self._data, key=lambda x: (x[0].value, x[1]))]


def default_registry() -> VenueRegistry:
    registry = VenueRegistry()
    research_fallbacks = lambda primary: FallbackMarketData(
        primary(), NormalizedMarketData(YahooFinanceClient()),
        NormalizedMarketData(OpenBBClient()))
    registry.register(MarketKind.CRYPTO_SPOT, "weex", lambda: research_fallbacks(WeexSpotMarketData))
    registry.register(MarketKind.CRYPTO_FUTURES, "weex", lambda: research_fallbacks(WeexFuturesMarketData))
    # OpenBB is the normalized research/data route for non-crypto markets.
    for market in (MarketKind.EQUITIES, MarketKind.FOREX, MarketKind.COMMODITIES):
        asset_class = {MarketKind.EQUITIES: "equity", MarketKind.FOREX: "currency",
                       MarketKind.COMMODITIES: "commodity"}[market]
        registry.register(market, "openbb", lambda kind=asset_class: FallbackMarketData(
            OpenBBClient(asset_class=kind), YahooFinanceClient()))
    registry.register(MarketKind.OPTIONS, "openbb", lambda: OpenBBClient(asset_class="equity"))
    # Yahoo is primary and OpenBB is the only fallback; WEEX is crypto-only.
    registry.register(MarketKind.INDIAN_INDICES, "openbb", lambda: FallbackMarketData(
        NormalizedMarketData(YahooFinanceClient()),
        NormalizedMarketData(OpenBBClient(asset_class="index"))))
    registry.register(MarketKind.BANKNIFTY_OPTIONS, "openbb", lambda: FallbackMarketData(
        NormalizedMarketData(YahooFinanceClient()),
        NormalizedMarketData(OpenBBClient(asset_class="index"))))
    return registry
