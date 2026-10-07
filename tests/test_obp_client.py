import json
from dataclasses import replace

import httpx
import pytest

from obp_sentinel.obp_client import OBPClient, OBPError, required_scopes


@pytest.fixture
def app_config(config):
    return replace(
        config,
        obp_base_url="http://obp",
        oidc_issuer="http://oidc/obp-oidc",
        oidc_client_id="sentinel-id",
        oidc_client_secret="sentinel-secret",
        log_levels=["error", "warning"],
    )


def make_client(config, handler):
    calls = []

    def record(request):
        calls.append(request)
        return handler(request)

    return OBPClient(config, httpx.Client(transport=httpx.MockTransport(record))), calls


def oidc(request):
    if request.url.path == "/obp-oidc/.well-known/openid-configuration":
        return httpx.Response(200, json={"token_endpoint": "http://oidc/obp-oidc/token"})
    if request.url.path == "/obp-oidc/token":
        assert request.headers["authorization"].startswith("Basic ")
        assert b"grant_type=client_credentials" in request.content
        return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
    return None


def test_log_cache_uses_an_application_token(app_config):
    def handler(request):
        if response := oidc(request):
            return response
        assert request.headers["authorization"] == "Bearer tok"
        return httpx.Response(200, json={"entries": [{"message": "a"}, {"message": "b"}]})

    client, calls = make_client(app_config, handler)
    assert client.log_cache("error", 10) == ["a", "b"]
    assert client.log_cache("error", 10) == ["a", "b"]
    assert [c.url.path for c in calls].count("/obp-oidc/token") == 1  # token reused while valid


def test_a_401_gets_a_new_token_once(app_config):
    rejected = []

    def handler(request):
        if response := oidc(request):
            return response
        if not rejected:
            rejected.append(request)
            return httpx.Response(401, json={})
        return httpx.Response(200, json={"meters": []})

    client, calls = make_client(app_config, handler)
    assert client.telemetry() == {"meters": []}
    assert [c.url.path for c in calls].count("/obp-oidc/token") == 2


def test_missing_credentials_are_reported(app_config):
    client, _ = make_client(replace(app_config, oidc_client_secret=""), oidc)
    with pytest.raises(OBPError, match="OIDC_CLIENT_SECRET"):
        client.telemetry()


def test_declares_one_scope_per_watched_level_telemetry_and_metrics(app_config):
    def handler(request):
        if response := oidc(request):
            return response
        assert request.method == "PUT"
        assert request.url.path == "/obp/v7.0.0/consumers/current/platform-app"
        return httpx.Response(200, json={"declared": json.loads(request.content)})

    client, _ = make_client(app_config, handler)
    sent = client.declare_platform_app()["declared"]
    assert [s["role_name"] for s in sent["required_scopes"]] == [
        "CanGetSystemLogCacheError", "CanGetSystemLogCacheWarning", "CanGetTelemetry", "CanReadAggregateMetrics",
        "CanGetReachableRoles",
    ]
    assert sent["required_scopes"] == required_scopes(app_config)
    assert all(s["bank_id"] == "" and s["needed_for"] for s in sent["required_scopes"])


def test_aggregate_metrics_asks_for_one_window(app_config):
    def handler(request):
        if response := oidc(request):
            return response
        assert request.url.path == "/obp/v6.0.0/management/aggregate-metrics"
        assert request.url.params["from_date"] == "2026-10-04T09:00:00.000Z"
        assert request.url.params["to_date"] == "2026-10-04T09:15:00.000Z"
        return httpx.Response(200, json={"count": 7, "average_response_time": 12.5})

    client, _ = make_client(app_config, handler)
    assert client.aggregate_metrics(1791104400, 1791105300) == {"count": 7, "average_response_time": 12.5}


def test_errors_name_the_full_url(config):
    def refuse(request):
        raise httpx.ConnectError("Connection refused", request=request)

    client = OBPClient(config, httpx.Client(transport=httpx.MockTransport(refuse)))
    with pytest.raises(OBPError, match=r"GET http://\S+/obp/v5\.1\.0/root failed: Connection refused"):
        client.root()


def test_an_older_obp_api_that_refuses_an_optional_scope_gets_the_required_ones(app_config):
    declared = []

    def handler(request):
        if response := oidc(request):
            return response
        roles = [s["role_name"] for s in json.loads(request.content)["required_scopes"]]
        declared.append(roles)
        if "CanGetReachableRoles" in roles:
            return httpx.Response(400, json={"message": "OBP-35048: Invalid Platform App declaration."})
        return httpx.Response(200, json={"required_scopes": []})

    client, _ = make_client(app_config, handler)
    client.declare_platform_app()
    assert "CanGetReachableRoles" in declared[0]
    assert declared[1] == [s["role_name"] for s in required_scopes(app_config) if not s["optional"]]
