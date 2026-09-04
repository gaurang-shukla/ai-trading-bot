import importlib.util
import os
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from tradebot.adapters import PaperclipReporter
from tradebot.app import app, integration_status
from tradebot.config import load_project_env
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
        "role": "multi-agent market decision",
    }


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
