from datetime import date, timedelta
import json
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from tradebot.app import app, integration_status
from tradebot.adapters import PaperclipReporter
from tradebot.banknifty_options import DhanOptionChainProvider, OptionChainService
from tradebot.models import MarketKind
from tradebot.weex_universe import WeexUniverseService


def ticker(symbol, status="trading"):
    return {"symbol": symbol, "status": status, "lastPrice": "1.2", "quoteVolume": "50"}


def test_complete_paginated_weex_universe_searches_after_record_1000():
    rows = [ticker(f"COIN{i}USDT") for i in range(1005)] + [ticker("TARGETUSDT")]
    def loader(_market, page, _cursor):
        start = (page - 1) * 400
        batch = rows[start:start + 400]
        return {"data": batch, "hasMore": start + 400 < len(rows), "nextCursor": str(page + 1)}
    service = WeexUniverseService(loader, ttl=60)
    universe = service.universe(MarketKind.CRYPTO_FUTURES)
    assert len(universe["instruments"]) == 1006
    assert universe["diagnostics"]["pages_retrieved"] == 3
    for query in ("TARGET", "target", "TARGETUSDT", "TARGET/USDT", "TARGET-USDT"):
        assert service.search(MarketKind.CRYPTO_FUTURES, query)["matches"][0]["symbol"] == "TARGETUSDT"


def test_weex_filters_and_deduplicates_without_crossing_markets():
    futures = [ticker("ABCUSDT"), ticker("ABCUSDT"), ticker("BADUSDT", "suspended")]
    spot = [ticker("SPOTUSDT")]
    service = WeexUniverseService(lambda market, *_: futures if market is MarketKind.CRYPTO_FUTURES else spot)
    result = service.universe(MarketKind.CRYPTO_FUTURES)
    assert [row["symbol"] for row in result["instruments"]] == ["ABCUSDT"]
    assert result["diagnostics"]["duplicates_removed"] == 1
    assert result["diagnostics"]["invalid_or_suspended_removed"] == 1
    assert service.search(MarketKind.CRYPTO_FUTURES, "SPOT")["found"] is False


def test_weex_duplicate_page_loop_and_last_known_good():
    calls = []
    fail = [False]
    def loader(_market, page, _cursor):
        calls.append(page)
        if fail[0]: raise TimeoutError()
        return {"data": [ticker("ABCUSDT")], "hasMore": True, "nextCursor": "same"}
    service = WeexUniverseService(loader, ttl=0, max_pages=10)
    assert service.universe(MarketKind.CRYPTO_SPOT)["diagnostics"]["pages_retrieved"] == 1
    fail[0] = True
    assert service.universe(MarketKind.CRYPTO_SPOT, refresh=True)["stale"] is True


def test_assets_search_endpoint_uses_only_mocked_genuine_universe():
    mocked = WeexUniverseService(lambda *_: [ticker("REALUSDT")])
    with patch("tradebot.app.weex_universes", mocked):
        payload = TestClient(app).get("/api/assets/search?market=crypto_futures&q=REAL").json()
    assert payload["found"] is True
    assert payload["matches"][0]["symbol"] == "REALUSDT"


class DhanTransport:
    def __init__(self): self.calls = []
    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        response = Mock(status_code=200, headers={})
        if "scrip-master" in url:
            response.text = "EXCH_ID,SEGMENT,INSTRUMENT,SYMBOL_NAME,SECURITY_ID\nNSE,I,INDEX,BANKNIFTY,13\n"
        elif "expirylist" in url:
            response.json.return_value = {"data": [(date.today() + timedelta(days=7)).isoformat()]}
        else:
            expiry = (date.today() + timedelta(days=7)).isoformat()
            response.json.return_value = {"data": {"last_price": 50000, "timestamp": "provider-time", "oc": {
                "50000": {"ce": {"ltp": 100, "volume": 2, "oi": 3, "top_bid_price": 99,
                                   "top_ask_price": 101, "greeks": {"delta": .5}},
                          "pe": {"ltp": 90, "volume": 4, "oi": 5, "top_bid_price": 89,
                                   "top_ask_price": 91}}}}}
        return response


def test_dhan_resolves_instrument_expiry_and_normalizes_without_live_network():
    transport = DhanTransport()
    provider = DhanOptionChainProvider("client", "token", transport=transport, sleep=lambda _: None)
    result = OptionChainService([provider]).option_chain()
    assert result["available"] is True
    assert result["provider"] == "DhanHQ"
    assert {row["option_type"] for row in result["contracts"]} == {"CE", "PE"}
    assert all(call[2]["headers"].get("access-token") == "token" for call in transport.calls if "scrip-master" not in call[1])


def test_paperclip_disabled_status_and_fail_open_idempotent_sanitized_event(monkeypatch):
    monkeypatch.setenv("PAPERCLIP_ENABLED", "false")
    monkeypatch.delenv("PAPERCLIP_TASK_BRIDGE_URL", raising=False)
    assert integration_status()["paperclip"]["status_label"] == "Optional · Off"
    sent = []
    class Response:
        def __enter__(self): return self
        def __exit__(self, *_): pass
    def transport(request, timeout):
        sent.append(json.loads(request.data)); return Response()
    monkeypatch.setenv("PAPERCLIP_ENABLED", "true")
    monkeypatch.setenv("PAPERCLIP_TASK_BRIDGE_URL", "http://bridge/events")
    reporter = PaperclipReporter(api_key="secret", transport=transport)
    event = {"event": "quick_signal_completed", "symbol": "BTCUSDT", "api_key": "must-not-leak"}
    assert reporter.report(event, "same")["delivered"] is True
    assert reporter.report(event, "same")["status"] == "duplicate"
    assert sent[0]["api_key"] == "[redacted]"
    failing = PaperclipReporter(api_key="secret", transport=lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError()), retries=1)
    assert failing.report({"event": "paper_position_opened"}, "failure")["status"] == "temporarily_unavailable"
