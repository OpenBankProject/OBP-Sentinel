from dataclasses import replace

import httpx

import pytest

from obp_sentinel import config as config_module
from obp_sentinel.config import Config


@pytest.fixture(autouse=True)
def app_directories(monkeypatch):
    """No test reaches a real OBP-API: an instance's app directory lists what a test puts here, by its URL."""
    listed: dict[str, dict[str, str]] = {}

    def get(url, timeout):
        base = url.removesuffix("/obp/v6.0.0/app-directory")
        if base not in listed:
            raise httpx.ConnectError("no OBP-API in tests")
        entries = [{"name": k, "value": v} for k, v in listed[base].items()]
        return httpx.Response(200, json={"app_directory": entries}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", get)
    monkeypatch.setattr(config_module, "_directories", {})
    return listed


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # so no real .env is read
    base = Config.from_env()
    return replace(
        base,
        db_path=str(tmp_path / "sentinel.db"),
        poll_seconds=120,
        log_levels=["error"],
        bucket_minutes=15,
        telemetry_prefixes=["obp.api."],
        ignore_regex="system/log-cache|management/telemetry|management/aggregate-metrics|consumers/current/platform-app",
        min_watch_hours=4,
        digest_size=3,
        max_per_week=10,
        min_priority=1.0,
        snooze_days=7,
    )
