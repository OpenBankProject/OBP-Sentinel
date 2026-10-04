from dataclasses import replace

import pytest

from obp_sentinel.config import Config


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
        ignore_regex="system/log-cache|management/telemetry|my/logins/direct",
        min_watch_hours=4,
        digest_size=3,
        max_per_week=10,
        min_priority=1.0,
        snooze_days=7,
    )
