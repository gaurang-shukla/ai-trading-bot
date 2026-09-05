from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tradebot.app import create_app, quick_results
from tradebot.models import Side
from tradebot.paper import PaperStore


def open_trade(store, side="LONG", price=100, notional=1_000):
    return store.open_position(
        market="equities", symbol="TEST", display_name="Test Asset", side=side,
        price=price, notional=notional, signal={"side": "BUY" if side == "LONG" else "SELL", "confidence": .8},
        risk_plan={"stop_loss": 90, "take_profit": 120, "risk_score": .2, "position_size_pct": .01},
    )


def test_account_initializes_and_persists(tmp_path: Path):
    path = tmp_path / "signal.db"
    assert PaperStore(path).account()["cash_balance"] == 100_000
    open_trade(PaperStore(path))
    assert PaperStore(path).account()["open_positions_count"] == 1
    assert PaperStore(path).account()["cash_balance"] == 99_000


@pytest.mark.parametrize(("side", "mark", "expected"), [("LONG", 110, 100), ("SHORT", 90, 100)])
def test_long_and_short_pnl(side, mark, expected, tmp_path: Path):
    store = PaperStore(tmp_path / f"{side}.db")
    position = open_trade(store, side)
    store.mark(position["id"], mark)
    assert store.positions()[0]["unrealized_pnl"] == expected
    trade = store.close_position(position["id"], mark, "Test target")
    assert trade["realized_pnl"] == expected
    assert trade["result"] == "win"
    assert store.account()["realized_pnl"] == expected


def test_invalid_price_and_notional_are_rejected(tmp_path: Path):
    store = PaperStore(tmp_path / "invalid.db")
    with pytest.raises(ValueError):
        open_trade(store, price=0)
    with pytest.raises(ValueError):
        open_trade(store, notional=100_001)


def test_watchlist_and_journal_crud(tmp_path: Path):
    store = PaperStore(tmp_path / "lists.db")
    store.add_watchlist({"market": "equities", "symbol": "aapl", "display_name": "Apple"})
    assert store.watchlist()[0]["symbol"] == "AAPL"
    assert store.delete_watchlist("equities", "AAPL")
    assert store.watchlist() == []
    note = store.add_note("Wait for confirmation", symbol="aapl")
    assert store.journal()[0] == note


def test_paper_routes_and_ui_are_present(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SIGNAL_DB_PATH", str(tmp_path / "routes.db"))
    paths = {route.path for route in create_app().routes}
    assert {"/api/paper/account", "/api/paper/positions", "/api/paper/trades",
            "/api/paper/watchlist", "/api/paper/journal"} <= paths
    app_js = Path("src/tradebot/web/app.js").read_text()
    html = Path("src/tradebot/web/index.html").read_text()
    assert "Open paper ${side}" in app_js
    assert "Paper trading only — no real money is used." in app_js
    assert 'href="/paper"' in html and "PAPER MODE" in html


def test_hold_requires_force_and_buy_opens_long(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SIGNAL_DB_PATH", str(tmp_path / "api.db"))
    client = TestClient(create_app())
    base = {"live_price": 50, "display_name": "Test Asset",
            "risk_plan": {"stop_loss": 45, "take_profit": 60, "risk_score": .3,
                          "position_size_pct": .01}}
    quick_results.put("equities", "HOLDTEST", {**base, "signal": {"side": Side.HOLD, "confidence": .5}})
    response = client.post("/api/paper/positions", json={"market": "equities", "symbol": "HOLDTEST"})
    assert response.status_code == 409

    quick_results.put("equities", "BUYTEST", {**base, "signal": {"side": Side.BUY, "confidence": .8}})
    response = client.post("/api/paper/positions", json={"market": "equities", "symbol": "BUYTEST",
                                                          "notional_amount": 500})
    assert response.status_code == 201
    assert response.json()["side"] == "LONG"
