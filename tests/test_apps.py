"""Watching an app (API Explorer II, the API Manager, the Portal) through its own /monitoring endpoints."""

from dataclasses import replace

import httpx

from obp_sentinel.collector import Collector
from obp_sentinel.config import Config
from obp_sentinel.health import health
from obp_sentinel.obp_client import OBPClient
from obp_sentinel.signatures import parse_line, signature_of
from obp_sentinel.store import Store


def test_an_instance_with_a_monitoring_path_is_an_app_that_keeps_all_its_telemetry(config, monkeypatch):
    monkeypatch.setenv("SENTINEL_INSTANCES", "local,explorer")
    monkeypatch.setenv("MONITORING_PATH", "/ignored")  # only taken prefixed: never makes OBP-API an app
    monkeypatch.setenv("EXPLORER_OBP_BASE_URL", "http://localhost:5173")
    monkeypatch.setenv("EXPLORER_MONITORING_PATH", "/api/monitoring/")
    monkeypatch.setenv("SENTINEL_TELEMETRY_PREFIXES", "obp.api.")  # OBP-API's: an app does not inherit them
    monkeypatch.setenv("SENTINEL_IGNORE_REGEX", "system/log-cache")
    local, explorer = Config.all_from_env()
    assert not local.is_app and local.telemetry_prefixes == ["obp.api."]
    assert explorer.is_app and explorer.monitoring_path == "/api/monitoring"
    assert explorer.telemetry_prefixes == [] and explorer.ignore_regex == "monitoring/"


def test_the_client_reads_the_apps_monitoring_endpoints_with_the_same_token(config):
    app = replace(config, obp_base_url="http://explorer", monitoring_path="/api/monitoring",
                  oidc_issuer="http://oidc", oidc_client_id="id", oidc_client_secret="secret")
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(200, json={"token_endpoint": "http://oidc/token"})
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        if request.url.path == "/api/monitoring/log-cache/error":
            return httpx.Response(200, json={"entries": [{"level": "ERROR", "message": "boom"}]})
        if request.url.path == "/api/monitoring/telemetry":
            return httpx.Response(200, json={"api_instance_id": "api-explorer-ii_x", "git_commit": "abc", "meters": []})
        return httpx.Response(404)

    client = OBPClient(app, httpx.Client(transport=httpx.MockTransport(handler)))
    assert client.log_cache("error", 10) == ["boom"]
    assert client.telemetry()["git_commit"] == "abc"
    monitoring = [c for c in calls if c.url.path.startswith("/api/monitoring")]
    assert [c.url.params.get("limit") for c in monitoring] == ["10", None]
    assert all(c.headers["authorization"] == "Bearer tok" for c in monitoring)


class AppClient:
    def log_cache(self, level, limit):
        return ["[2026-10-10T20:16:18.632Z] [ERROR] [Proxy] GET /obp/v6.0.0/banks/gh.29.uk → 500"]

    def telemetry(self):
        return {"api_instance_id": "obp-manager_x", "git_commit": "1a2b3c4",
                "meters": [{"name": "manager.http.requests", "type": "timer", "tags": {"route": "/status"},
                            "measurements": {"count": 3}}]}

    def __getattr__(self, name):  # root, metrics, reach and Scope declaration are OBP-API's only
        raise AssertionError(f"an app has no {name}")


def test_an_app_is_polled_for_logs_and_telemetry_only(config, tmp_path):
    app = replace(config, monitoring_path="/monitoring", telemetry_prefixes=[], ignore_regex="monitoring/")
    store = Store(str(tmp_path / "app.db"))
    Collector(app, store, AppClient()).poll_once()
    assert [r["source"] for r in store.db.execute("SELECT source FROM polls ORDER BY id")] == ["log:error", "telemetry"]
    assert store.db.execute("SELECT COUNT(*) FROM telemetry_values").fetchone()[0] == 1
    assert store.db.execute("SELECT git_commit FROM deployments").fetchone()[0] == "1a2b3c4"
    [sig] = store.db.execute("SELECT logger, endpoint FROM signatures").fetchall()
    assert tuple(sig) == ("Proxy", "/obp/v6.0.0/banks/{id}")


def test_javascript_errors_are_recognised():
    for raw, exception in [
        ("Role check: could not ask OBP-API: TypeError: fetch failed\n    at node:internal/deps/undici:13510:13", "TypeError"),
        ("Error fetching resource docs Error: connect ECONNREFUSED 127.0.0.1:8080\n    at TCPConnectWrap", "Error"),
        ("[2026-10-10T20:16:18.632Z] [ERROR] [Opey] OBPAPIError: OBP-20006", "OBPAPIError"),
    ]:
        assert signature_of("error", parse_line(raw)).exception == exception
    a = signature_of("error", parse_line("TypeError: fetch failed\n    at a (x.js:1:2)"))
    b = signature_of("error", parse_line("TypeError: fetch failed\n    at b (y.js:9:9)"))
    assert a.id == b.id  # the stack is not part of the signature


def test_an_app_refusing_for_lack_of_a_role_says_which_scopes_to_grant(config, tmp_path):
    app = replace(config, monitoring_path="/monitoring")
    store = Store(str(tmp_path / "app.db"))
    store.record_poll("log:error", ok=False, error='GET http://manager/monitoring/log-cache/error failed (403): '
                      '{"error":"Requires one of the Roles: CanGetSystemLogCacheError, CanGetSystemLogCacheAll"}')
    [problem] = health(store, app)["problems"]
    assert problem["key"] == "missing-scopes"
    assert "CanGetSystemLogCacheError (or CanGetSystemLogCacheAll)" in problem["action"]
