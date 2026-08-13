from __future__ import annotations

import logging

from libs.venice_sdk.client import VeniceClient


def test_venice_client_coerces_base_url_argument(caplog):
    caplog.set_level(logging.WARNING, logger="venice_sdk.client")
    client = VeniceClient(base_url="https://api.venice.ai/api/v1")
    assert client.config.base_url.endswith("/api/v1")
    assert "missing '/api/v1'" not in caplog.text

    caplog.clear()
    client = VeniceClient(base_url="https://api.venice.ai/api")
    assert client.config.base_url.endswith("/api/v1")
    assert "missing '/api/v1'" in caplog.text


def test_venice_client_env_normalization(monkeypatch, caplog):
    monkeypatch.setenv("VENICE_API_BASE_URL", "https://example.com/venice")
    caplog.set_level(logging.WARNING, logger="venice_sdk.client")
    client = VeniceClient()
    assert client.config.base_url.endswith("/api/v1")
    assert "missing '/api/v1'" in caplog.text
