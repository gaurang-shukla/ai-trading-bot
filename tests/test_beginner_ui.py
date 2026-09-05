from pathlib import Path

from tradebot.models import MarketKind
from tradebot.overview import SEED_UNIVERSES, YAHOO_SYMBOLS


APP_JS = Path("src/tradebot/web/app.js").read_text()


def test_generic_options_is_hidden_and_direct_route_is_clear():
    assert "{id:'options'" not in APP_JS
    assert "NOT CONNECTED YET" in APP_JS
    assert "No blank assets or generated option rows are shown." in APP_JS


def test_commodity_display_labels_and_beginner_descriptions_exist():
    from tradebot.assets import COMMODITIES
    expected = {"CL": "Crude Oil", "ZC": "Corn", "GC": "Gold", "NG": "Natural Gas",
                "ZW": "Wheat", "SI": "Silver", "HG": "Copper"}
    for symbol, name in expected.items():
        assert COMMODITIES[symbol].display_name == name
        assert COMMODITIES[symbol].instrument_type == "Commodity futures"
        assert COMMODITIES[symbol].description.startswith(f"{name} futures")
    # Commodity copy comes from the API rather than a duplicate frontend mapping.
    assert "commodityDescriptions" not in APP_JS
    assert "quick.description" in APP_JS


def test_internal_commodity_symbols_remain_provider_symbols():
    symbols = SEED_UNIVERSES[MarketKind.COMMODITIES]
    assert symbols == ["GC", "SI", "CL", "NG", "HG", "ZC", "ZW"]
    assert all(YAHOO_SYMBOLS[symbol] == f"{symbol}=F" for symbol in symbols)
    assert "body:JSON.stringify({market,venue:venueFor(market),symbol,equity:100000})" in APP_JS


def test_normalized_asset_identity_is_shared_across_market_surfaces():
    from tradebot.assets import asset_metadata

    expected = {
        "AAPL": ("AAPL", "US equity"),
        "NVDA": ("NVDA", "US equity"),
        "HDFCBANK.NS": ("HDFC Bank", "Indian equity"),
        "RELIANCE.NS": ("Reliance", "Indian equity"),
        "INFY.NS": ("Infosys", "Indian equity"),
    }
    for symbol, (name, kind) in expected.items():
        metadata = asset_metadata(MarketKind.EQUITIES, symbol)
        assert (metadata.display_name, metadata.instrument_type) == (name, kind)
        assert metadata.provider_symbol == symbol
    assert "NVDA shares" not in APP_JS
    assert "assetIdentity(market,row)" in APP_JS
    assert "assetIdentity(market,r)" in APP_JS
    assert "paperAssetLink(x)" in APP_JS


def test_clean_asset_names_cover_every_market_without_row_level_provider_noise():
    from tradebot.assets import asset_metadata

    expected = (
        (MarketKind.CRYPTO_SPOT, "BTCUSDT", "BTC/USDT", "BTCUSDT"),
        (MarketKind.CRYPTO_FUTURES, "ETHUSDT", "ETH/USDT", "ETHUSDT"),
        (MarketKind.FOREX, "GBPUSD", "GBP/USD", "GBPUSD=X"),
        (MarketKind.COMMODITIES, "NG", "Natural Gas", "NG=F"),
        (MarketKind.EQUITIES, "NVDA", "NVDA", "NVDA"),
        (MarketKind.EQUITIES, "SBIN.NS", "SBI", "SBIN.NS"),
        (MarketKind.INDIAN_INDICES, "ICICIBANK.NS", "ICICI Bank", "ICICIBANK.NS"),
    )
    for market, symbol, display_name, provider_symbol in expected:
        metadata = asset_metadata(market, symbol)
        assert metadata.display_name == display_name
        assert metadata.provider_symbol == provider_symbol

    identity = APP_JS[APP_JS.index("function assetIdentity"):APP_JS.index("function paperAssetLink")]
    assert "provider_symbol" not in identity
    assert "instrument_type" not in identity
    assert "<small>" not in identity


def test_compact_refresh_control_sits_with_search_and_keeps_safety_states():
    assert 'class="search-row">${refreshControl' in APP_JS
    assert 'class="refresh-icon"' in APP_JS
    assert 'aria-label="${safe(label)}"' in APP_JS
    assert "classList.add('is-refreshing')" in APP_JS
    assert "button.disabled=remaining>0" in APP_JS
    assert "Date.now()+30000" in APP_JS
    assert "?refresh=true" in APP_JS
    assert "Couldn’t refresh right now. Showing last available data." in APP_JS
