import importlib.util
import os
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from tradebot.adapters import PaperclipReporter
from tradebot.app import app, integration_status, startup_diagnostics
from tradebot.config import load_project_env
from tradebot.diagnostics import diagnostics
from tradebot.models import MarketKind
from tradebot.weex import WeexCredentials


client = TestClient(app)


def test_app_shell_and_status_are_available():
    page = client.get("/")
    assert page.status_code == 200
    assert "OpenBB data" in page.text

    status = client.get("/api/status")
    assert status.status_code == 200
    assert set(status.json()["integrations"]) == {
        "openbb", "tradingagents", "paperclip", "weex"
    }
    assert all("ready" in item for item in status.json()["integrations"].values())

    debug = client.get("/debug")
    assert debug.status_code == 200
    assert set(debug.json()) == {"openai", "tradingagents", "openbb", "weex", "yahoo", "paperclip"}
    assert debug.headers["cache-control"] == "no-store"
    assert "last_success" in debug.json()["openai"]
    assert "last_traceback" not in debug.json()["openai"]


def test_debug_bypasses_frontend_and_service_worker(capsys):
    response = client.get("/debug", headers={"accept": "text/html"})
    assert response.headers["content-type"].startswith("application/json")
    worker = client.get("/sw.js").text
    assert "url.pathname!=='/debug'" in worker
    startup_diagnostics()
    output = capsys.readouterr().out
    for label in ("OpenAI model:", "OpenAI key loaded:", "TradingAgents imported:",
                  "TradingAgents version:", "Configured LLM:", "Paperclip enabled:",
                  "WEEX enabled:", "OpenBB enabled:"):
        assert label in output


def test_debug_returns_the_complete_last_traceback_only_when_enabled(monkeypatch):
    try:
        raise RuntimeError("diagnostic failure")
    except RuntimeError as exc:
        diagnostics.failure("tradingagents", exc)
    monkeypatch.setenv("SIGNAL_DEBUG", "true")
    body = client.get("/debug").json()["tradingagents"]
    assert body["last_error"] == "RuntimeError: diagnostic failure"
    assert "raise RuntimeError(\"diagnostic failure\")" in body["last_traceback"]


def test_project_env_loading_does_not_depend_on_working_directory(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "WEEX_API_KEY=from-project-env\n"
        "WEEX_SECRET_KEY=secret\n"
        "WEEX_PASSPHRASE=passphrase\n"
    )
    with patch.dict(os.environ, {}, clear=True), \
            patch("tradebot.config.ENV_FILE", env_file), \
            patch("pathlib.Path.cwd", return_value=Path("/tmp")):
        load_project_env()
        assert WeexCredentials.from_env() == WeexCredentials(
            "from-project-env", "secret", "passphrase"
        )


def test_external_integrations_use_configuration_readiness():
    configured = {
        "OPENBB_API_URL": "http://openbb:6900",
        "PAPERCLIP_BRIDGE_TOKEN": "inbound-secret",
        "OPENAI_API_KEY": "test-key",
    }
    with patch.dict(os.environ, configured, clear=True), \
            patch.object(importlib.util, "find_spec", return_value=object()):
        status = integration_status()
    assert status["openbb"]["ready"] is True
    assert status["tradingagents"]["ready"] is True
    assert status["paperclip"]["ready"] is True


def test_paperclip_api_url_alone_is_not_ready():
    with patch.dict(os.environ, {"PAPERCLIP_API_URL": "http://paperclip:3100"}, clear=True):
        status = integration_status()["paperclip"]
    assert status["configured"] is False
    assert status["ready"] is False


def test_paperclip_task_bridge_requires_url_and_key():
    for incomplete in (
        {"PAPERCLIP_TASK_BRIDGE_URL": "http://bridge/events"},
        {"PAPERCLIP_API_KEY": "outbound-secret"},
    ):
        with patch.dict(os.environ, incomplete, clear=True):
            assert integration_status()["paperclip"]["ready"] is False

    with patch.dict(os.environ, {
        "PAPERCLIP_TASK_BRIDGE_URL": "http://bridge/events",
        "PAPERCLIP_API_KEY": "outbound-secret",
    }, clear=True):
        assert integration_status()["paperclip"]["ready"] is True


def test_missing_paperclip_is_a_noop(monkeypatch):
    monkeypatch.delenv("PAPERCLIP_TASK_BRIDGE_URL", raising=False)
    monkeypatch.delenv("PAPERCLIP_API_KEY", raising=False)
    monkeypatch.setattr("tradebot.adapters.urlopen", lambda *args, **kwargs:
                        (_ for _ in ()).throw(AssertionError("network must not be called")))
    reporter = PaperclipReporter()
    assert reporter.configured is False
    assert reporter.report({"event": "analysis"}) is None


def test_tradingagents_states_remain_independent():
    with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}, clear=True), \
            patch.object(importlib.util, "find_spec", return_value=None):
        status = integration_status()["tradingagents"]
    assert status == {
        "installed": False,
        "configured": True,
        "ready": False,
        "role": "optional advanced research layer",
    }


def test_home_status_describes_architecture_and_shows_unconfigured_paperclip():
    javascript = client.get("/assets/app.js").text
    with patch.dict(os.environ, {}, clear=True):
        status = integration_status()
    assert status["paperclip"]["ready"] is False
    assert status["paperclip"]["role"] == "optional control and audit bridge"
    assert status["openbb"]["role"] == "market data and research layer"
    assert status["tradingagents"]["role"] == "optional advanced research layer"
    assert status["weex"]["role"] == "crypto venue and data source"
    assert "item.enabled" not in javascript
    assert "'Not configured'" in javascript


def test_market_registry_is_exposed():
    response = client.get("/api/markets")
    assert response.status_code == 200
    assert {item["market"] for item in response.json()} >= {
        "crypto_spot", "crypto_futures", "equities", "forex", "commodities"
    }


def test_paperclip_bridge_fails_closed_without_token():
    response = client.post("/api/paperclip/analyze", json={"runId": "run-1"})
    assert response.status_code == 401


def test_non_crypto_overview_exposes_real_universe_without_fake_quotes():
    populated = {
        "market": "equities", "source": "OpenBB", "fear_greed": {"score": 55},
        "summary": {"assets": 1, "advancers_pct": 100, "total_quote_volume": 10},
        "gainers": [], "losers": [],
        "assets": [{"symbol": "AAPL", "price": 100, "change": 1,
                    "volume": 10, "signal_score": 70}],
    }
    with patch("tradebot.app.market_overview", return_value=populated):
        response = client.get("/api/overview/equities")
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "OpenBB"
    assert body["assets"][0]["price"] == 100


def test_overview_api_forwards_manual_refresh_flag():
    with patch("tradebot.app.market_overview", return_value={"last_updated": "now"}) as overview:
        response = client.get("/api/overview/equities?refresh=true")
    assert response.status_code == 200
    overview.assert_called_once_with(MarketKind.EQUITIES, refresh=True)


def test_market_and_ranking_pages_include_one_shared_refresh_control():
    script = client.get("/assets/app.js").text
    assert "Refresh market" in script
    assert "Refresh rankings" in script
    assert "Last updated:" in script
    assert "?refresh=true" in script
    assert "Refreshing…" in script
    assert "Refresh available in ${remaining}s" in script
    assert "Couldn’t refresh right now. Showing last available data." in script
