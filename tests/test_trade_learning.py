import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tradebot.app import create_app
from tradebot.paper import PaperStore


def setup(store, symbol="LEARN", **quick):
    data = {
        "signal": {"side": "BUY", "confidence": .85, "rationale": "Trend aligned."},
        "risk_plan": {"stop_loss": 99, "take_profit": 120, "risk_score": .3, "position_size_pct": .02},
        "setup_refreshed_at": "2026-01-01T00:00:00+00:00", "provider_data_timestamp": "2026-01-01T00:00:01Z",
        "chart_default_timeframe": "1h", "timeframe_breakdown": [{"timeframe": "1h", "signal": "bullish"}],
        "key_levels": {"support": 95, "resistance": 101}, "atr_pct": 5, "change_24h": 10,
        "volume": 1000, "funding_rate": .001,
    }
    data.update(quick)
    return store.open_position(market="equities", symbol=symbol, display_name=symbol, side="LONG",
                               price=100, notional=1000, signal=data, risk_plan=data["risk_plan"])


def test_snapshot_is_full_immutable_and_secret_free(tmp_path):
    store = PaperStore(tmp_path / "snapshot.db")
    quick = {"api_key": "never", "nested": {"authorization": "never"}}
    position = setup(store, **quick)
    saved = position["signal_snapshot"]
    quick["atr_pct"] = 1
    assert saved["entry_price"] == 100 and saved["amount_invested"] == 1000
    assert saved["multi_timeframe_rows"][0]["timeframe"] == "1h"
    assert saved["atr_pct"] == 5
    assert "never" not in json.dumps(saved)


@pytest.mark.parametrize(("price", "reason", "result", "follow"), [
    (90, "stop_loss", "loss", "yes"), (120, "take_profit", "win", "yes"),
    (105, "manual", "win", "manual_review_required"),
])
def test_close_generates_review(price, reason, result, follow, tmp_path):
    store = PaperStore(tmp_path / f"{reason}.db")
    position = setup(store)
    trade = store.close_position(position["id"], price, reason)
    review = store.reviews(trade_id=trade["id"])[0]
    assert review["result"] == result and review["did_follow_plan"] == follow
    assert review["amount_invested"] == 1000 and review["exit_value"] == trade["exit_value"]
    assert review["realized_return_pct"] == pytest.approx(trade["realized_pnl_pct"])


def test_deterministic_traps_and_unknown(tmp_path):
    store = PaperStore(tmp_path / "traps.db")
    position = setup(store, timeframe_breakdown=[{"signal": "bullish"}, {"signal": "bearish"}])
    review = store.regenerate_review(store.close_position(position["id"], 90, "stop_loss")["id"])
    assert {"late_entry_after_pump", "high_atr_volatility", "stop_too_tight",
            "weak_multi_timeframe_confirmation"} <= set(review["trap_categories"])
    position = store.open_position(market="equities", symbol="UNKNOWN", display_name="Unknown", side="LONG",
        price=100, notional=1000, signal={"side": "BUY"}, risk_plan={})
    review = store.reviews(trade_id=store.close_position(position["id"], 90, "stop_loss")["id"])[0]
    assert review["trap_category"] == "unknown"


def test_learning_analytics_and_api(tmp_path, monkeypatch):
    monkeypatch.setenv("SIGNAL_DB_PATH", str(tmp_path / "api.db"))
    client = TestClient(create_app())
    store = client.app.state.paper_store
    first = setup(store, symbol="WIN")
    win = store.close_position(first["id"], 120, "take_profit")
    second = setup(store, symbol="LOSS")
    loss = store.close_position(second["id"], 90, "stop_loss")
    learning = client.get("/api/paper/learning").json()
    assert learning["reviewed_trades"] == 2 and learning["win_rate"] == 50
    assert learning["average_return_pct"] == 5
    assert learning["profit_factor"] == 2
    assert "80–90" in learning["confidence_bucket_performance"]
    assert "26–50" in learning["risk_score_bucket_performance"]
    assert learning["small_sample_warning"]
    assert len(client.get("/api/paper/reviews").json()) == 2
    assert client.get(f"/api/paper/reviews/{win['id']}").status_code == 200
    assert client.post(f"/api/paper/reviews/{loss['id']}/regenerate").status_code == 200


def test_learning_endpoints_have_stable_empty_responses(tmp_path, monkeypatch):
    monkeypatch.setenv("SIGNAL_DB_PATH", str(tmp_path / "empty-api.db"))
    client = TestClient(create_app())
    assert client.get("/api/paper/reviews").json() == []
    empty = client.get("/api/paper/learning").json()
    filtered = client.get("/api/paper/learning?symbol=DOTUSDT").json()
    assert empty["reviewed_trades"] == filtered["reviewed_trades"] == 0
    assert empty["confidence_bucket_performance"] == {}


def test_old_trade_without_snapshot_fields_does_not_crash(tmp_path):
    store = PaperStore(tmp_path / "old.db")
    position = store.open_position(market="equities", symbol="OLD", display_name="Old", side="LONG",
        price=100, notional=1000, signal={"side": "BUY"}, risk_plan={})
    trade = store.close_position(position["id"], 90)
    assert store.regenerate_review(trade["id"])["trap_category"] == "unknown"


def test_learning_ui_sections_are_present():
    source = Path("src/tradebot/web/app.js").read_text()
    assert "Trade Reviews" in source and "Learning Analytics" in source
    assert "Past paper performance for this asset" in source and "Learning note" in source
    assert "Learning data is not available yet." in source
    assert "No paper reviews yet." in source
    assert "querySelectorAll('.paper-inline-error')" in source
    assert "getJSON('/api/paper/reviews').catch(()=>[])" in source
