import time
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from tradebot.app import app
from tradebot.banknifty_options import NSEOptionChainClient, atm_strike, build_chain
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
    with (patch("tradebot.app.OpenBBClient.option_chain", side_effect=RuntimeError("not supported")),
          patch("tradebot.app.NSEOptionChainClient.option_chain", side_effect=RuntimeError("blocked"))):
        response = client.get("/api/banknifty-options")
    assert response.status_code == 200
    assert response.json()["message"] == "Real Bank Nifty option-chain data is temporarily unavailable."
    assert response.json()["explanation"]
    assert response.json()["last_checked"]
    assert response.json()["contracts"] == []
    assert response.json()["available"] is False
    assert response.json()["provider_attempts"] == {"openbb": True, "nse_fallback": True}
    assert response.json()["failure_category"] == "nse_http_error"
    assert response.json()["provider_status"] == "nse_http_error"
    assert "provider_errors" not in response.json()


def test_banknifty_provider_errors_are_sanitized_in_debug(monkeypatch):
    monkeypatch.setenv("SIGNAL_DEBUG", "true")
    with (patch("tradebot.app.OpenBBClient.option_chain", side_effect=RuntimeError("private openbb detail")),
          patch("tradebot.app.NSEOptionChainClient.option_chain", side_effect=RuntimeError("private nse detail"))):
        payload = client.get("/api/banknifty-options").json()
    assert "provider_errors" not in payload
    text = str(payload["provider_diagnostics"])
    assert "private openbb detail" not in text
    assert "private nse detail" not in text


def test_banknifty_ui_keeps_attempt_metadata_in_compact_details():
    javascript = client.get("/assets/app.js").text
    assert '<details class="provider-attempts"><summary>Provider diagnostics</summary>' in javascript
    assert "data.provider_diagnostics?" in javascript
    assert "Live option-chain provider required" in javascript
    assert "Connect live options provider" in javascript
    assert "Import option-chain CSV for demo/research" in javascript
    assert "retry-banknifty" in javascript
    assert "No fake rows shown" in javascript
    assert "No fake rows are generated." in javascript


def test_openbb_empty_response_triggers_nse_fallback():
    nse = {"source": "NSE fallback", "underlying_price": 51020, "contracts": RAW["contracts"]}
    with (patch("tradebot.app.OpenBBClient.option_chain", return_value={"contracts": []}),
          patch("tradebot.app.NSEOptionChainClient.option_chain", return_value=nse) as fallback):
        response = client.get("/api/banknifty-options")
    assert response.status_code == 200
    assert response.json()["available"] is True
    assert response.json()["provider_status"] == "connected"
    assert response.json()["source"] == "NSE fallback"
    assert response.json()["contracts"]
    fallback.assert_called_once_with(None)


def test_nse_response_normalizes_to_option_contract(monkeypatch):
    payload = {"records": {"underlyingValue": 51020, "expiryDates": ["10-Sep-2026"], "data": [{
        "expiryDate": "10-Sep-2026", "strikePrice": 51000,
        "CE": {"lastPrice": 200, "change": 3, "totalTradedVolume": 100,
               "openInterest": 1000, "impliedVolatility": 18, "bidprice": 199,
               "askPrice": 201}}]}}
    client_instance = NSEOptionChainClient(retries=0)
    initial = Mock(status_code=200, url="https://www.nseindia.com/", headers={})
    initial.raise_for_status = Mock()
    response = Mock(status_code=200, url="https://www.nseindia.com/api/option-chain-indices?symbol=BANKNIFTY",
                    headers={"content-type": "application/json"})
    response.raise_for_status = Mock()
    response.json.return_value = payload
    client_instance.session.get = Mock(side_effect=[initial, response])
    raw = client_instance.option_chain()
    result = build_chain(raw, raw["underlying_price"])
    contract = result["contracts"][0]
    assert contract["expiry"] == "10-Sep-2026"
    assert contract["option_type"] == "CE"
    assert contract["strike"] == 51000
    assert contract["last_price"] == 200
    assert contract["open_interest"] == 1000
    assert contract["underlying_price"] == 51020
    assert contract["moneyness"] == "ATM"
    assert contract["distance_from_spot_pct"] == -0.0392


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
    provider.snapshot.return_value = MarketSnapshot("RELIANCE.NS", 1200, "2026-09-04T00:00:00Z", "test")
    registry = Mock()
    registry.market_data.return_value = provider
    with patch("tradebot.app.default_registry", return_value=registry):
        response = client.post("/api/analyze/deep", json={"market": MarketKind.EQUITIES.value,
            "venue": "openbb", "symbol": "RELIANCE.NS", "equity": 100000, "refresh": True})
        job_id = response.json()["job_id"]
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            result = client.get(f"/api/analyze/deep/status/{job_id}").json()
            if result["status"] not in {"queued", "running"}:
                break
            time.sleep(.01)
    assert response.status_code == 200
    assert result["status"] == "failed"
    assert result["fallback_result"]["signal"]
    assert "Quick Signal remains available" in result["user_friendly_error"]
    assert "debug_error" not in result


def test_empty_provider_attempts_are_distinguished_without_fabricated_rows():
    with (patch("tradebot.app.OpenBBClient.option_chain", return_value={"contracts": []}),
          patch("tradebot.app.NSEOptionChainClient.option_chain", return_value={"contracts": [], "underlying_price": None})):
        payload = client.get("/api/banknifty-options").json()
    assert payload["failure_category"] == "nse_empty_chain"
    assert "valid option chain could not be retrieved" in payload["explanation"]
    assert payload["provider_attempts"] == {"openbb": True, "nse_fallback": True}
    assert payload["contracts"] == []


def _response(status=200, payload=None, content_type="application/json"):
    response = Mock(status_code=status, url="https://www.nseindia.com/api/option-chain-indices?symbol=BANKNIFTY",
                    headers={"content-type": content_type})
    response.raise_for_status = Mock()
    if payload is None:
        response.json.side_effect = ValueError("not json")
    else:
        response.json.return_value = payload
    return response


def test_openbb_connection_refused_has_precise_category():
    from tradebot.banknifty_options import classify_openbb_failure
    assert classify_openbb_failure(ConnectionRefusedError(61, "Connection refused")) == "openbb_connection_refused"


def test_nse_block_statuses_are_precisely_classified():
    for status in (401, 403, 429):
        nse = NSEOptionChainClient(retries=0)
        nse.session.get = Mock(return_value=_response(status, {}))
        try:
            nse.option_chain()
        except Exception as exc:
            assert exc.category == "nse_blocked_by_provider"


def test_nse_html_and_empty_json_have_precise_categories():
    initial = _response(200, {})
    initial.url = "https://www.nseindia.com/"
    for response, expected in [(_response(200, None, "text/html"), "nse_html_instead_of_json"),
                               (_response(200, {"records": {"data": []}}), "nse_empty_chain")]:
        nse = NSEOptionChainClient(retries=0)
        nse.session.get = Mock(side_effect=[initial, response])
        try:
            nse.option_chain()
        except Exception as exc:
            assert exc.category == expected


def test_diagnostics_are_hidden_unless_debug_enabled(monkeypatch):
    monkeypatch.delenv("SIGNAL_DEBUG", raising=False)
    with (patch("tradebot.app.OpenBBClient.option_chain", side_effect=ConnectionRefusedError()),
          patch("tradebot.app.NSEOptionChainClient.option_chain", side_effect=RuntimeError("down"))):
        assert "provider_diagnostics" not in client.get("/api/banknifty-options").json()
    monkeypatch.setenv("SIGNAL_DEBUG", "true")
    with (patch("tradebot.app.OpenBBClient.option_chain", side_effect=ConnectionRefusedError()),
          patch("tradebot.app.NSEOptionChainClient.option_chain", side_effect=RuntimeError("down"))):
        diagnostics = client.get("/api/banknifty-options").json()["provider_diagnostics"]
    assert set(diagnostics[0]) >= {"provider", "attempted", "status_code", "final_url", "content_type",
                                  "got_json", "raw_row_count", "ce_count", "pe_count",
                                  "normalized_contract_count", "failure_category", "sanitized_error"}
