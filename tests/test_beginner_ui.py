from pathlib import Path

from tradebot.models import MarketKind
from tradebot.overview import SEED_UNIVERSES, YAHOO_SYMBOLS


APP_JS = Path("src/tradebot/web/app.js").read_text()


def test_generic_options_is_hidden_and_direct_route_is_clear():
    assert "{id:'options'" not in APP_JS
    assert "NOT CONNECTED YET" in APP_JS
    assert "No blank assets or generated option rows are shown." in APP_JS


def test_commodity_display_labels_and_beginner_descriptions_exist():
    expected = {"CL": "Crude Oil", "ZC": "Corn", "GC": "Gold", "NG": "Natural Gas",
                "ZW": "Wheat", "SI": "Silver", "HG": "Copper"}
    for symbol, name in expected.items():
        assert f"{symbol}:'{symbol} ({name})'" in APP_JS
        assert f"{symbol}:'{name} futures" in APP_JS


def test_internal_commodity_symbols_remain_provider_symbols():
    symbols = SEED_UNIVERSES[MarketKind.COMMODITIES]
    assert symbols == ["GC", "SI", "CL", "NG", "HG", "ZC", "ZW"]
    assert all(YAHOO_SYMBOLS[symbol] == f"{symbol}=F" for symbol in symbols)
    assert "body:JSON.stringify({market,venue:venueFor(market),symbol,equity:100000})" in APP_JS
