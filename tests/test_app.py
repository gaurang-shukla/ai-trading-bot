import importlib.util
import os
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

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
        "PAPERCLIP_API_URL": "http://paperclip:3100",
        "OPENAI_API_KEY": "test-key",
    }
    with patch.dict(os.environ, configured, clear=True), \
            patch.object(importlib.util, "find_spec", return_value=object()):
        status = integration_status()
    assert status["openbb"]["ready"] is True
    assert status["tradingagents"]["ready"] is True
    assert status["paperclip"]["ready"] is True


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
    response = client.get("/api/overview/equities")
    assert response.status_code == 200
    body = response.json()
    assert body["source"].startswith("OpenBB")
    assert body["assets"][0]["price"] is None
