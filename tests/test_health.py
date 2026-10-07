from obp_sentinel.health import diagnose, health
from obp_sentinel.store import Store

JWKS = ('GET https://dcr.example/obp/v5.1.0/system/log-cache/error failed (401): {"code":401,"message":"OBP-20200: The '
        'application cannot be identified.  <- OBP-20206: Bad JSON Object Signing and Encryption (JOSE) exception."}')
ROLES = ('GET https://sandbox.example/obp/v5.1.0/system/log-cache/error failed (403): {"code":403,"message":"OBP-20006: '
         'User is missing one or more roles: CanGetSystemLogCacheError or CanGetSystemLogCacheAll"}')
METRICS_ROLE = ('GET https://sandbox.example/obp/v6.0.0/management/aggregate-metrics failed (403): {"code":403,"message":'
                '"OBP-20006: User is missing one or more roles: CanReadAggregateMetrics"}')
NOT_SERVED = ('GET https://sandbox.example/obp/v7.0.0/management/telemetry failed (404): {"code":404,"message":"OBP-10404: '
              '404 Not Found."}')
DECLARE_404 = NOT_SERVED.replace("GET", "PUT").replace("management/telemetry", "consumers/current/platform-app")


def test_diagnoses(config):
    assert diagnose("log:error", JWKS, config)["key"] == "token-rejected"
    assert "oauth2.jwk_set.url" in diagnose("log:error", JWKS, config)["action"]
    assert diagnose("log:error", ROLES, config)["roles"] == [["CanGetSystemLogCacheError", "CanGetSystemLogCacheAll"]]
    d = diagnose("telemetry", NOT_SERVED, config)
    assert d["key"] == "not-served" and "v7.0.0/management/telemetry" in d["title"] and "OBP_TELEMETRY_API_VERSION" in d["action"]
    assert diagnose("platform-app", DECLARE_404, config).get("warn")
    assert diagnose("telemetry", "GET https://x/obp failed: [Errno 111] Connection refused", config)["key"] == "unreachable"
    assert diagnose("log:error", "OIDC_ISSUER, OIDC_CLIENT_ID and OIDC_CLIENT_SECRET must be set", config)["key"] == "settings"


def test_same_cause_is_one_problem_and_scopes_are_merged(config):
    store = Store(config.db_path)
    for source, error in (("log:error", JWKS), ("log:warning", JWKS), ("telemetry", JWKS)):
        store.record_poll(source, ok=False, error=error)
    store.set_state("platform_app", {"ts": 1, "ok": False, "error": JWKS.replace("GET", "PUT")})
    h = health(store, config)
    assert [p["key"] for p in h["problems"]] == ["token-rejected"]
    assert sorted(h["problems"][0]["sources"]) == ["Error log", "Scope declaration", "Telemetry", "Warning log"]
    assert h["more_may_follow"]

    for source, error in (("log:error", ROLES), ("log:warning", ROLES.replace("Error", "Warning")),
                          ("telemetry", NOT_SERVED), ("metrics", METRICS_ROLE)):
        store.record_poll(source, ok=False, error=error)
    store.set_state("platform_app", {"ts": 1, "ok": False, "error": DECLARE_404})
    h = health(store, config)
    keys = [p["key"] for p in h["problems"]]
    assert keys == ["missing-scopes", "not-served", "not-served"] and not h["more_may_follow"]
    scopes = h["problems"][0]["action"]
    assert "CanGetSystemLogCacheError (or CanGetSystemLogCacheAll)" in scopes and "CanReadAggregateMetrics" in scopes

    for source in ("log:error", "log:warning", "telemetry", "metrics"):
        store.record_poll(source, ok=True)
    store.set_state("platform_app", {"ts": 1, "ok": True, "consumer_id": "c1", "missing": []})
    assert health(store, config)["problems"] == []
    store.close()


def test_declared_missing_scopes_are_listed_with_the_consumer(config):
    store = Store(config.db_path)
    store.set_state("platform_app", {"ts": 1, "ok": True, "consumer_id": "c1", "missing": ["CanGetTelemetry"]})
    [p] = health(store, config)["problems"]
    assert p["key"] == "missing-scopes" and "Consumer c1" in p["action"] and "CanGetTelemetry" in p["action"]
    store.close()
