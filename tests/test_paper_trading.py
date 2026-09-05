from pathlib import Path
from unittest.mock import Mock

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
    assert "function paperTradeCopy(market,action)" in app_js
    assert "Paper trading only. No real money is used." in app_js
    assert 'href="/paper"' in html and "PAPER MODE" in html


@pytest.mark.parametrize(("market", "action", "button", "helper"), [
    ("crypto_futures", "BUY", "Open paper LONG", "futures price goes up"),
    ("crypto_futures", "SELL", "Open paper SHORT", "futures price goes down"),
    ("equities", "BUY", "Buy paper shares", "Simulates buying shares"),
    ("equities", "SELL", "Simulate paper short", "margin/borrowing"),
    ("crypto_spot", "SELL", "Simulate bearish paper trade", "already owning the asset"),
    ("forex", "BUY", "Buy base currency paper trade", "first currency in the pair"),
    ("forex", "SELL", "Sell base currency paper trade", "first currency in the pair"),
    ("banknifty_options", "BUY CE", "Buy paper Call option / Buy CE", "Call option (CE)"),
    ("banknifty_options", "BUY PE", "Buy paper Put option / Buy PE", "Put option (PE)"),
])
def test_market_aware_paper_trade_copy_is_present(market, action, button, helper):
    javascript = Path("src/tradebot/web/app.js").read_text()
    assert market in javascript
    assert action in javascript
    assert button in javascript
    assert helper in javascript


def test_hold_ui_offers_watchlist_without_an_open_trade_button():
    javascript = Path("src/tradebot/web/app.js").read_text()
    helper = javascript.split("function paperTradeCopy", 1)[1].split("function paperActions", 1)[0]
    actions = javascript.split("function paperActions", 1)[1].split("function bindPaperActions", 1)[0]
    assert "['HOLD','WATCH','AVOID']" in helper
    assert "active:false,button:'Add to watchlist',helper:'No active trade setup'" in helper
    assert "No active trade setup — watch support/resistance for confirmation." in actions
    assert "copy.active?`<button id=\"open-paper\"" in actions


def test_dashboard_explains_internal_sides_and_uses_friendly_labels():
    javascript = Path("src/tradebot/web/app.js").read_text()
    assert "LONG means the paper trade benefits if price rises" in javascript
    assert "SHORT means it benefits if price falls" in javascript
    assert "Paper shares" in javascript
    assert "Simulated paper short" in javascript
    assert "Paper futures ${side.toLowerCase()}" in javascript
    assert "Internal side: ${safe(x.side)}" in javascript


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


def test_paper_frontend_requests_use_valid_fetch_init_objects():
    javascript = Path("src/tradebot/web/app.js").read_text()

    # Passing getJSON directly to map supplied Array.map's numeric index as
    # fetch's RequestInit argument, which browsers reject with a TypeError.
    assert ".map(getJSON)" not in javascript
    assert ".map(url=>getJSON(url))" in javascript
    assert "async function getJSON(url, options={})" in javascript
    assert "async function postJSON(url,payload)" in javascript
    assert "method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)" in javascript
    assert "postJSON('/api/paper/positions',{market,symbol,side,notional_amount:Number(input.value)})" in javascript
    assert "postJSON(`/api/paper/positions/${b.dataset.id}/close`,{close_reason:'manual'})" in javascript
    assert "deleteJSON(`/api/paper/watchlist/${b.dataset.market}/${encodeURIComponent(b.dataset.symbol)}`)" in javascript
    assert "async function deleteJSON(url){return getJSON(url,{method:'DELETE'});}" in javascript


def test_paper_dashboard_has_specific_redacted_error_state():
    javascript = Path("src/tradebot/web/app.js").read_text()
    paper_error = javascript.split("function paperErrorView", 1)[1].split("async function home", 1)[0]
    paper_page = javascript.split("async function paperPage", 1)[1].split("function paperPositionTable", 1)[0]

    assert "We couldn’t load Paper Trading" in paper_error
    assert "Paper Trading is local-only. Please refresh or restart the app." in paper_error
    assert "We couldn’t load this market" not in paper_error
    assert "error.message" not in paper_error
    assert "catch(error){paperErrorView(error)}" in paper_page


def test_all_paper_api_routes_support_the_dashboard_flow(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SIGNAL_DB_PATH", str(tmp_path / "dashboard.db"))
    provider = Mock(snapshot=Mock(return_value=Mock(price=110)))
    registry = Mock(market_data=Mock(return_value=provider))
    monkeypatch.setattr("tradebot.app.default_registry", lambda: registry)
    client = TestClient(create_app())
    quick_results.put("equities", "FLOWTEST", {
        "live_price": 100,
        "display_name": "Flow Test",
        "signal": {"side": Side.BUY, "confidence": .8},
        "risk_plan": {"stop_loss": 90, "take_profit": 120, "risk_score": .2,
                      "position_size_pct": .01},
    })

    assert client.get("/paper").status_code == 200
    assert client.get("/api/paper/account").status_code == 200
    assert client.get("/api/paper/positions").json() == []
    opened = client.post("/api/paper/positions", json={
        "market": "equities", "symbol": "FLOWTEST", "notional_amount": 1_000,
    })
    assert opened.status_code == 201
    position_id = opened.json()["id"]
    assert len(client.get("/api/paper/positions").json()) == 1

    closed = client.post(f"/api/paper/positions/{position_id}/close", json={
        "close_reason": "Dashboard test",
    })
    assert closed.status_code == 200
    assert len(client.get("/api/paper/trades").json()) == 1

    watched = client.post("/api/paper/watchlist", json={
        "market": "equities", "symbol": "FLOWTEST", "display_name": "Flow Test",
    })
    assert watched.status_code == 201
    assert len(client.get("/api/paper/watchlist").json()) == 1
    assert client.delete("/api/paper/watchlist/equities/FLOWTEST").status_code == 204

    note = client.post("/api/paper/journal", json={"note": "Review the setup", "symbol": "FLOWTEST"})
    assert note.status_code == 201
    assert len(client.get("/api/paper/journal").json()) == 1


def test_paper_asset_links_cover_positions_trades_and_watchlist():
    javascript = Path("src/tradebot/web/app.js").read_text()
    assert 'href="/asset/${safe(x.market)}/${encodeURIComponent(x.symbol)}"' in javascript
    assert "<td>${paperAssetLink(x)}</td>" in javascript
    assert "<div class=\"paper-list\">${paperAssetLink(x)}" in javascript
    assert javascript.count("<td>${paperAssetLink(x)}</td>") == 2
    assert "market.startsWith('crypto_')&&symbol.endsWith('USDT')" in javascript
    assert "market==='forex'&&symbol.length===6" in javascript


def test_close_button_stops_navigation_and_quantity_is_bounded():
    javascript = Path("src/tradebot/web/app.js").read_text()
    close_handler = javascript.split("document.querySelectorAll('.close-paper')", 1)[1].split("document.querySelectorAll('.remove-watch')", 1)[0]
    assert "event.preventDefault();event.stopPropagation()" in close_handler
    assert "Qty ${quantity(x.quantity)}" in javascript
    assert "Math.abs(n)>=1?4:Math.abs(n)>=.001?6:8" in javascript


def test_multiple_trades_reserve_cash_and_reconcile_equity(tmp_path: Path):
    store = PaperStore(tmp_path / "account-math.db", starting_cash=10_000)
    long = open_trade(store, "LONG", 100, 1_000)
    short = store.open_position(
        market="equities", symbol="SHORTTEST", display_name="Short Test", side="SHORT",
        price=200, notional=2_000, signal={"side": "SELL", "confidence": .7},
        risk_plan={"stop_loss": 220, "take_profit": 170},
    )
    assert store.account()["cash_balance"] == 7_000
    store.mark(long["id"], 110)
    store.mark(short["id"], 180)
    assert store.account()["unrealized_pnl"] == pytest.approx(300)
    assert store.account()["equity"] == pytest.approx(10_300)
    store.close_position(long["id"], 110)
    store.close_position(short["id"], 220)
    account = store.account()
    assert account["cash_balance"] == pytest.approx(9_900)
    assert account["equity"] == pytest.approx(9_900)
    assert account["realized_pnl"] == pytest.approx(-100)
    assert account["win_rate"] == 50
    assert account["closed_trades_count"] == 2


def test_duplicate_position_and_second_close_are_blocked(tmp_path: Path):
    store = PaperStore(tmp_path / "lifecycle.db")
    position = open_trade(store)
    with pytest.raises(ValueError, match="already exists"):
        open_trade(store)
    store.close_position(position["id"], 100)
    with pytest.raises(KeyError):
        store.close_position(position["id"], 100)
    assert store.account()["cash_balance"] == 100_000
    assert store.account()["closed_trades_count"] == 1


def test_tiny_prices_and_non_finite_numbers_are_handled(tmp_path: Path):
    store = PaperStore(tmp_path / "tiny.db")
    position = open_trade(store, price=1e-12, notional=1_000)
    assert position["quantity"] == pytest.approx(1e15)
    for bad in (float("nan"), float("inf"), -1):
        with pytest.raises(ValueError):
            store.mark(position["id"], bad)


def test_signal_snapshot_preserves_trade_context_but_removes_secrets(tmp_path: Path):
    store = PaperStore(tmp_path / "secrets.db")
    position = store.open_position(
        market="equities", symbol="SAFE", display_name="Safe", side="LONG", price=10,
        notional=100, signal={"side": "BUY", "api_key": "do-not-store", "nested": {"token": "no"}},
        risk_plan={"stop_loss": 9, "take_profit": 12, "authorization": "Bearer no"},
    )
    encoded = __import__("json").dumps(position["signal_snapshot"])
    assert "do-not-store" not in encoded and "Bearer no" not in encoded
    assert position["stop_loss"] == 9 and position["take_profit"] == 12


def test_duplicate_watchlist_keeps_single_original_entry(tmp_path: Path):
    store = PaperStore(tmp_path / "watch.db")
    first = store.add_watchlist({"market": "forex", "symbol": "eurusd", "display_name": "EUR/USD"})
    second = store.add_watchlist({"market": "forex", "symbol": "EURUSD", "display_name": "Euro / Dollar"})
    assert len(store.watchlist()) == 1
    assert second["added_at"] == first["added_at"]
    assert second["display_name"] == "Euro / Dollar"


def test_banknifty_paper_api_is_research_only(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SIGNAL_DB_PATH", str(tmp_path / "banknifty-paper.db"))
    response = TestClient(create_app()).post("/api/paper/positions", json={
        "market": "banknifty_options", "symbol": "BANKNIFTY", "notional_amount": 1_000,
    })
    assert response.status_code == 409
    assert "research-only" in response.json()["detail"]


def test_frontend_guards_paper_mutations_against_double_submit():
    javascript = Path("src/tradebot/web/app.js").read_text()
    assert "if(confirm.disabled)return;confirm.disabled=true" in javascript
    assert "if(b.disabled)return;b.disabled=true" in javascript
    assert "if(submit.disabled)return;submit.disabled=true" in javascript
    assert "friendlyPaperError" in javascript
    assert "['Closed trades',account.closed_trades_count,'count']" in javascript


def test_corrupt_database_is_quarantined_without_overwriting_it(tmp_path: Path):
    path = tmp_path / "broken" / "signal.db"
    path.parent.mkdir()
    original = b"not a sqlite database"
    path.write_bytes(original)
    store = PaperStore(path)
    assert store.account()["cash_balance"] == 100_000
    assert store.recovered_database is not None
    assert store.recovered_database.read_bytes() == original


def test_separate_stores_serialize_cash_reservations(tmp_path: Path):
    from concurrent.futures import ThreadPoolExecutor

    path = tmp_path / "concurrent.db"
    stores = [PaperStore(path, starting_cash=1_000) for _ in range(2)]

    def attempt(index):
        try:
            return stores[index].open_position(
                market="equities", symbol=f"RACE{index}", display_name="Race", side="LONG",
                price=10, notional=750, signal={"side": "BUY"}, risk_plan={},
            )
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt, range(2)))
    assert sum(result is not None for result in results) == 1
    assert PaperStore(path).account()["cash_balance"] == 250


@pytest.mark.parametrize(("side", "mark", "reason"), [
    ("LONG", 90, "stop_loss"), ("LONG", 120, "take_profit"),
    ("SHORT", 110, "stop_loss"), ("SHORT", 80, "take_profit"),
])
def test_paper_trigger_boundaries(side, mark, reason, tmp_path: Path):
    store = PaperStore(tmp_path / f"trigger-{side}-{reason}.db")
    position = open_trade(store, side)
    store.mark(position["id"], mark)
    marked = store.positions()[0]
    assert store.trigger_reason(marked) == reason
    assert marked["position_status"] != "Active"


def test_position_values_account_capital_and_closed_values(tmp_path: Path):
    store = PaperStore(tmp_path / "values.db", starting_cash=10_000)
    position = open_trade(store, notional=1_000)
    store.mark(position["id"], 110)
    position = store.positions()[0]
    assert position["entry_notional"] == position["amount_invested"] == 1_000
    assert position["current_value"] == pytest.approx(1_100)
    assert position["allocated_pct"] == 10
    account = store.account()
    assert account["available_paper_cash"] == 9_000
    assert account["capital_in_open_trades"] == 1_000
    assert account["equity"] == 10_100
    trade = store.close_position(position["id"], 110, "manual")
    assert trade["entry_notional"] == 1_000
    assert trade["exit_value"] == pytest.approx(1_100)
    assert store.trades()[0]["position_status"] == "Closed manually"


def test_unavailable_mark_retains_price_and_is_visible(tmp_path: Path):
    store = PaperStore(tmp_path / "unavailable.db")
    position = open_trade(store)
    store.mark(position["id"], 105)
    store.mark_unavailable(position["id"])
    marked = store.positions()[0]
    assert marked["current_price"] == 105
    assert marked["position_status"] == "Price unavailable"


def test_dashboard_names_capital_and_status_fields():
    javascript = Path("src/tradebot/web/app.js").read_text()
    assert "Available paper cash" in javascript
    assert "Capital in open trades" in javascript
    assert "<th>Amount invested</th>" in javascript
    assert "position_status" in javascript
    assert "['Cash balance'" not in javascript


def test_recent_exits_are_visible_before_open_positions_with_complete_history():
    javascript = Path("src/tradebot/web/app.js").read_text()
    assert javascript.index("Recent paper exits") < javascript.index("OPEN POSITIONS")
    assert "paperTradeTable(trades.slice(0,5)" in javascript
    assert "Amount invested / Exit value" in javascript
    assert "Realized P&amp;L" in javascript
    assert "Status / Reason" in javascript
    assert "Opened / Closed" in javascript


@pytest.mark.parametrize(("reason", "label"), [
    ("stop_loss", "Closed by stop loss"),
    ("take_profit", "Closed by take profit"),
    ("manual", "Closed manually"),
])
def test_closed_trade_history_preserves_reason_values_and_amounts(tmp_path, reason, label):
    store = PaperStore(tmp_path / f"history-{reason}.db")
    position = open_trade(store)
    trade = store.close_position(position["id"], 95, reason)
    history = store.trades()
    assert len(history) == store.account()["closed_trades_count"] == 1
    assert history[0]["close_reason"] == reason
    assert history[0]["position_status"] == label
    assert history[0]["amount_invested"] == trade["entry_notional"] == 1_000
    assert history[0]["exit_value"] == trade["exit_value"]
    assert history[0]["signal_snapshot"]


@pytest.mark.parametrize(("mark", "reason", "exit_price"), [
    (80, "stop_loss", 90), (130, "take_profit", 120),
])
def test_account_refresh_auto_closes_trigger_once(mark, reason, exit_price, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SIGNAL_DB_PATH", str(tmp_path / f"auto-{reason}.db"))
    monkeypatch.setenv("PAPER_AUTO_CLOSE_STOPS", "true")
    provider = Mock(snapshot=Mock(return_value=Mock(price=mark)))
    monkeypatch.setattr("tradebot.app.default_registry", lambda: Mock(market_data=Mock(return_value=provider)))
    client = TestClient(create_app())
    store = client.app.state.paper_store
    open_trade(store)

    assert client.get("/api/paper/account").status_code == 200
    assert client.get("/api/paper/account").status_code == 200
    trades = store.trades()
    assert len(trades) == 1
    assert trades[0]["close_reason"] == reason
    assert trades[0]["exit_price"] == exit_price


def test_disabled_auto_close_keeps_breach_with_warning(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SIGNAL_DB_PATH", str(tmp_path / "disabled-auto.db"))
    monkeypatch.setenv("PAPER_AUTO_CLOSE_STOPS", "false")
    provider = Mock(snapshot=Mock(return_value=Mock(price=90)))
    monkeypatch.setattr("tradebot.app.default_registry", lambda: Mock(market_data=Mock(return_value=provider)))
    client = TestClient(create_app())
    open_trade(client.app.state.paper_store)
    positions = client.get("/api/paper/positions").json()
    assert len(positions) == 1
    assert positions[0]["position_status"] == "Stop loss breached"
