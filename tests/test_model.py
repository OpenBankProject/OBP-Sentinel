import tomllib

import pytest

from senti.config import Config, ConfigError, setup_from_env
from senti.model import SetupError, load, parse, to_toml
from senti.home import in_home

SETUP = """
[system]
name = "obp"

[[service]]
name = "obp-api"
kind = "obp-api"
repository = "https://github.com/OpenBankProject/OBP-API"
source = "/src/OBP-API"
sonarcloud = "OpenBankProject_OBP-API"

[[service]]
name = "portal"
kind = "sveltekit"
repository = "https://github.com/OpenBankProject/OBP-Frontend"
source = "/src/OBP-Frontend"

[[environment]]
name = "local"

[[environment]]
name = "production"

[[instance]]
service = "obp-api"
environment = "production"
url = "https://api.example.com/"
credentials = "PRODUCTION_OIDC"

[[instance]]
id = "local"
service = "obp-api"
environment = "local"
url = "http://localhost:8080"
credentials = "LOCAL_OIDC"
discover = false
"""


def test_a_setup_is_read_with_ids_from_service_and_environment():
    setup = parse(tomllib.loads(SETUP))
    production, local = setup.instances
    assert (production.id, production.url, production.discover) == ("obp-api-production", "https://api.example.com", True)
    assert (local.id, local.discover) == ("local", False)
    assert setup.service("obp-api").github_repo == "OpenBankProject/OBP-API"
    assert [e.name for e in setup.environments] == ["local", "production"]


def test_written_setup_reads_back_the_same():
    setup = parse(tomllib.loads(SETUP))
    assert parse(tomllib.loads(to_toml(setup))) == setup


@pytest.mark.parametrize("change, error", [
    (lambda t: t.replace('kind = "sveltekit"', 'kind = "cobol"'), "kind 'cobol' is not one Senti knows"),
    (lambda t: t.replace('environment = "production"', 'environment = "prod"'), "no environment 'prod'"),
    (lambda t: t.replace('credentials = "LOCAL_OIDC"', 'credentials = "local"'), "must be the prefix"),
    (lambda t: t.replace('id = "local"', 'id = "obp-api-production"'), "same name: obp-api-production"),
    (lambda t: t.replace('service = "obp-api"\nenvironment = "local"', 'service = "portal"\nenvironment = "local"'),
     "a sveltekit service is read from Prometheus or Loki"),
    (lambda t: t + "\n[grafana]\nurl = 'x'\n", "Unknown section grafana"),
])
def test_mistakes_in_the_setup_are_explained(change, error):
    with pytest.raises(SetupError, match=error):
        parse(tomllib.loads(change(SETUP)))


def test_no_file_means_no_setup(tmp_path):
    assert load(tmp_path / "senti.toml") is None


def test_instances_come_from_senti_toml_when_it_is_there(config, monkeypatch, tmp_path, app_directories):
    (tmp_path / "senti.toml").write_text(SETUP)
    monkeypatch.setenv("SENTINEL_INSTANCES", "ignored")
    monkeypatch.setenv("PRODUCTION_OIDC_CLIENT_ID", "client")
    monkeypatch.setenv("PRODUCTION_OIDC_ISSUER", "https://idp.example.com/")
    app_directories["https://api.example.com"] = {"public_obp_portal_url": "https://portal.example.com"}
    app_directories["http://localhost:8080"] = {"public_obp_portal_url": "http://localhost:5174"}
    production, portal, local = Config.all_from_env()  # local discovers nothing: discover = false
    assert (production.name, production.service, production.kind, production.environment) == (
        "obp-api-production", "obp-api", "obp-api", "production")
    assert (production.oidc_client_id, production.oidc_issuer, production.oidc_client_setting) == (
        "client", "https://idp.example.com", "PRODUCTION_OIDC_CLIENT_ID")
    assert (production.obp_api_source, production.sonarcloud_project) == ("/src/OBP-API", "OpenBankProject_OBP-API")
    assert production.fix_worktree == "/src/OBP-API-sentinel-fixes"
    assert (portal.name, portal.service, portal.kind, portal.environment) == (
        "obp-api-production-portal", "portal", "sveltekit", "production")
    assert (portal.obp_base_url, portal.obp_api_source, portal.obp_api_url) == (
        "https://portal.example.com", "/src/OBP-Frontend", "https://api.example.com")
    assert (local.name, local.environment, local.db_path) == ("local", "local", in_home("sentinel-local.db"))


def test_without_senti_toml_each_instance_is_its_own_environment(config, monkeypatch, app_directories):
    monkeypatch.setenv("SENTINEL_INSTANCES", "local")
    monkeypatch.setenv("LOCAL_OBP_BASE_URL", "http://localhost:8080")
    app_directories["http://localhost:8080"] = {"public_obp_api_explorer_url": "http://localhost:5173"}
    local, explorer = Config.all_from_env()
    assert (local.service, local.kind, local.environment) == ("obp-api", "obp-api", "local")
    assert (explorer.service, explorer.kind, explorer.environment) == ("api-explorer", "vue-express", "local")


def test_import_env_keeps_each_instance_name_as_its_id(config, monkeypatch):
    monkeypatch.setenv("SENTINEL_INSTANCES", "local,ogcr")
    monkeypatch.setenv("OBP_API_SOURCE", "/src/OBP-API")
    monkeypatch.setenv("FRONTEND_SOURCE", "/src/OBP-Frontend")
    monkeypatch.setenv("SONARCLOUD_PROJECT", "OpenBankProject_OBP-API")
    for prefix in ("LOCAL", "OGCR"):
        monkeypatch.setenv(f"{prefix}_OBP_BASE_URL", f"https://{prefix.lower()}.example.com")
        monkeypatch.setenv(f"{prefix}_OIDC_CLIENT_ID", "id")
        monkeypatch.setenv(f"{prefix}_OIDC_CLIENT_SECRET", "secret")
    monkeypatch.setenv("LOCAL_OIDC_ISSUER", "http://localhost:9000/obp-oidc")
    setup, missing = setup_from_env()
    assert missing == ["OGCR_OIDC_ISSUER"]
    assert [(i.id, i.environment, i.url, i.credentials) for i in setup.instances] == [
        ("local", "local", "https://local.example.com", "LOCAL_OIDC"),
        ("ogcr", "ogcr", "https://ogcr.example.com", "OGCR_OIDC")]
    obp_api = setup.service("obp-api")
    assert (obp_api.kind, obp_api.source, obp_api.repository, obp_api.sonarcloud) == (
        "obp-api", "/src/OBP-API", "https://github.com/OpenBankProject/OBP-API", "OpenBankProject_OBP-API")
    assert [s.name for s in setup.services] == ["obp-api", "api-manager", "portal", "cohort", "sandbox-populator"]
    assert parse(tomllib.loads(to_toml(setup))).instances == setup.instances


def test_import_env_refuses_when_senti_toml_exists(config, tmp_path):
    (tmp_path / "senti.toml").write_text(SETUP)
    with pytest.raises(ConfigError, match="exists already"):
        setup_from_env()
