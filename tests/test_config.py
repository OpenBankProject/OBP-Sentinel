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


# The OBP-API versions Sentinel builds its paths from: each setting, with the default it has.
VERSION_SETTINGS = {
    "OBP_LOG_CACHE_API_VERSION": ("log_cache_api_version", "v5.1.0"),
    "OBP_TELEMETRY_API_VERSION": ("telemetry_api_version", "v7.0.0"),
    "OBP_AGGREGATE_METRICS_API_VERSION": ("aggregate_metrics_api_version", "v6.0.0"),
    "OBP_ROOT_API_VERSION": ("root_api_version", "v5.1.0"),
}


@pytest.mark.parametrize("setting", VERSION_SETTINGS)
def test_a_version_setting_has_a_default_of_v510_or_newer(config, monkeypatch, setting):
    monkeypatch.delenv(setting, raising=False)
    field, default = VERSION_SETTINGS[setting]
    assert getattr(Config.from_env(), field) == default


@pytest.mark.parametrize("setting", VERSION_SETTINGS)
@pytest.mark.parametrize("version", ["v5.0.0", "v4.0.0", "v3.1.0", "v1.2.1", "v5.0.9"])
def test_a_version_below_v510_is_refused_and_the_setting_is_named(config, monkeypatch, setting, version):
    monkeypatch.setenv(setting, version)
    with pytest.raises(ConfigError, match=f"{setting}.*{version}.*v5.1.0"):
        Config.from_env()


@pytest.mark.parametrize("setting", VERSION_SETTINGS)
@pytest.mark.parametrize("version", ["v5.1.0", "v5.1.1", "v5.2.0", "v6.0.0", "v7.0.0", "v10.0.0"])
def test_a_version_of_v510_or_newer_is_accepted_as_written(config, monkeypatch, setting, version):
    monkeypatch.setenv(setting, version)
    assert getattr(Config.from_env(), VERSION_SETTINGS[setting][0]) == version


@pytest.mark.parametrize("version", ["latest", "5.1.0", "v5.1", "v6", "", " v6.0.0", "v6.0.0/../v4.0.0", "v6.0.0/x"])
def test_something_that_is_not_a_version_is_refused(config, monkeypatch, version):
    monkeypatch.setenv("OBP_ROOT_API_VERSION", version)
    with pytest.raises(ConfigError, match="OBP_ROOT_API_VERSION"):
        Config.from_env()


def test_the_version_of_an_instance_is_checked_under_the_name_it_was_set_with(config, monkeypatch):
    monkeypatch.setenv("SENTINEL_INSTANCES", "local, staging")
    monkeypatch.setenv("STAGING_OBP_TELEMETRY_API_VERSION", "v4.0.0")
    with pytest.raises(ConfigError, match="STAGING_OBP_TELEMETRY_API_VERSION.*v4.0.0"):
        Config.all_from_env()


def test_an_instance_that_takes_the_plain_setting_is_checked_under_the_plain_name(config, monkeypatch):
    monkeypatch.setenv("SENTINEL_INSTANCES", "local")
    monkeypatch.setenv("OBP_LOG_CACHE_API_VERSION", "v3.1.0")
    with pytest.raises(ConfigError, match=r"^OBP_LOG_CACHE_API_VERSION|\bOBP_LOG_CACHE_API_VERSION\b.*v3.1.0"):
        Config.all_from_env()


def test_a_version_that_was_not_set_is_never_refused(config, monkeypatch):
    for setting in VERSION_SETTINGS:
        monkeypatch.delenv(setting, raising=False)
    Config.from_env()
