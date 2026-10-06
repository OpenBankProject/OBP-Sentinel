import pytest

from obp_sentinel.config import Config, ConfigError


def test_one_default_instance_without_a_list(config, monkeypatch):
    monkeypatch.delenv("SENTINEL_INSTANCES", raising=False)
    [c] = Config.all_from_env()
    assert c.name == "default" and c.db_path == "sentinel.db" and c.digest_dir == "digests"


def test_each_instance_takes_prefixed_settings_or_the_plain_ones(config, monkeypatch):
    monkeypatch.setenv("SENTINEL_INSTANCES", "local, staging-eu")
    monkeypatch.setenv("OBP_BASE_URL", "http://localhost:8080")
    monkeypatch.setenv("STAGING_EU_OBP_BASE_URL", "https://staging.example.com")
    monkeypatch.setenv("STAGING_EU_OBP_API_SOURCE", "/src/staging")
    local, staging = Config.all_from_env()
    assert (local.name, local.obp_base_url, local.db_path) == ("local", "http://localhost:8080", "sentinel-local.db")
    assert staging.obp_base_url == "https://staging.example.com" and staging.obp_api_source == "/src/staging"
    assert staging.digest_dir == "digests/staging-eu"


def test_bad_instance_names_are_refused(config, monkeypatch):
    monkeypatch.setenv("SENTINEL_INSTANCES", "Local Box")
    with pytest.raises(ConfigError):
        Config.all_from_env()


def test_instances_never_share_a_database(config, monkeypatch):
    monkeypatch.setenv("SENTINEL_INSTANCES", "a,b")
    monkeypatch.setenv("SENTINEL_DB", "sentinel.db")
    monkeypatch.setenv("B_SENTINEL_DB", "/data/b.db")
    a, b = Config.all_from_env()
    assert (a.db_path, b.db_path) == ("sentinel-a.db", "/data/b.db")
