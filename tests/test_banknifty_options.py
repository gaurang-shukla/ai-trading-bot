from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from tradebot.app import app
from tradebot.banknifty_options import atm_strike, build_chain
from tradebot.models import MarketKind, MarketSnapshot

client = TestClient(app)

RAW = {"contracts": [
    {"expiration": "2026-09-10", "strike": 51000, "option_type": "call", "last_price": 200,
     "change": 3, "volume": 100, "open_interest": 1000, "iv": 18, "bid": 199, "ask": 201},
    {"expiration": "2026-09-10", "strike": 51000, "option_type": "put", "last_price": 180,
     "change": -2, "volume": 80, "open_interest": 900},
    {"expiration": "2026-09-10", "strike": 51500, "option_type": "call", "last_price": 90,
     "volume": 40, "open_interest": 500},
]}


def test_banknifty_options_route_exists():
    assert client.get("/market/banknifty_options").status_code == 200
    assert {"market": "banknifty_options", "venue": "openbb"} in client.get("/api/markets").json()


def test_unavailable_provider_state_does_not_crash_or_return_fake_rows():
    with patch("tradebot.app.OpenBBClient.option_chain", side_effect=RuntimeError("not supported")):
        response = client.get("/api/banknifty-options")
    assert response.status_code == 200
    assert response.json()["message"] == "Bank Nifty options data provider not configured yet."
    assert response.json()["contracts"] == []
    assert response.json()["available"] is False


def test_ce_pe_and_moneyness_filtering():
    calls = build_chain(RAW, 51020, option_type="CE")
    puts = build_chain(RAW, 51020, option_type="PE")
    assert calls["contracts"] and all(row["option_type"] == "CE" for row in calls["contracts"])
    assert puts["contracts"] and all(row["option_type"] == "PE" for row in puts["contracts"])
    assert all(row["moneyness"] == "ATM" for row in build_chain(RAW, 51020, money="ATM")["contracts"])


def test_atm_strike_calculation_and_score_schema():
    assert atm_strike([50500, 51000, 51500], 51250) == 51000
    result = build_chain(RAW, 51020)
    score = result["contracts"][0]["score"]
    assert set(score) == {"signal", "confidence", "risk_score", "reason",
                          "suggested_stop_loss", "suggested_target"}
    assert score["signal"] in {"BUY CE", "BUY PE", "WATCH", "AVOID"}
    assert result["underlying_symbol"] == "^NSEBANK"


def test_indian_deep_ai_missing_ohlcv_has_clean_fallback():
    provider = Mock()
    provider.candles.return_value = []
    registry = Mock()
    registry.market_data.return_value = provider
    with patch("tradebot.app.default_registry", return_value=registry):
        response = client.post("/api/analyze/deep", json={"market": MarketKind.EQUITIES.value,
            "venue": "openbb", "symbol": "RELIANCE.NS", "equity": 100000, "refresh": True})
    assert response.status_code == 200
    assert response.json()["ai_available"] is False
    assert response.json()["ai_notice"] == (
        "Deep AI unavailable for this Indian asset because provider OHLCV data is incomplete. "
        "Quick Signal remains available.")
