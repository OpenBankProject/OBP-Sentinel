"""The endpoints Sentinel reads must refuse a call with no token: checked hourly, shown on the page."""

from dataclasses import replace

import httpx

from obp_sentinel.collector import SECURED, Collector
from obp_sentinel.obp_client import OBPClient
from obp_sentinel.store import Store


def check(config, answer):
    """The collector's check, against an instance answering each path with `answer(path)`; and the paths asked."""
    asked = []

    def handler(request):
        asked.append(request)
        status = answer(request.url.path)
        if status is None:
            raise httpx.ConnectError("down")
        return httpx.Response(status)

    store = Store(config.db_path)
    collector = Collector(config, store, OBPClient(config, httpx.Client(transport=httpx.MockTransport(handler))))
    collector.check_secured()
    return store.get_state(SECURED), asked, collector


def test_obp_api_is_secured_when_its_log_cache_telemetry_and_metrics_refuse_without_a_token(config):
    state, asked, _ = check(replace(config, obp_base_url="http://obp", log_levels=["error"]), lambda path: 401)
    assert state["verdict"] == "secured"
    assert [r.url.path for r in asked] == [
        f"/obp/{config.log_cache_api_version}/system/log-cache/error",
        f"/obp/{config.log_cache_api_version}/system/log-cache/all",
        f"/obp/{config.telemetry_api_version}/management/telemetry",
        f"/obp/{config.aggregate_metrics_api_version}/management/aggregate-metrics"]
    assert not any("authorization" in r.headers for r in asked)


def test_an_app_is_open_when_any_monitoring_endpoint_answers_without_a_token(config):
    app = replace(config, obp_base_url="http://explorer", monitoring_path="/api/monitoring", log_levels=["error"])
    state, asked, _ = check(app, lambda path: 200 if path.endswith("/telemetry") else 403)
    assert state["verdict"] == "open"
    assert [c for c in state["checks"] if c["status"] == 200] == [{"path": "/api/monitoring/telemetry", "status": 200}]


def test_it_cannot_tell_when_an_endpoint_is_missing_or_does_not_answer(config):
    app = replace(config, obp_base_url="http://portal", monitoring_path="/monitoring", log_levels=["error"])
    assert check(app, lambda path: 404 if path.endswith("/all") else 401)[0]["verdict"] == "unknown"
    app = replace(app, db_path="other.db")
    assert check(app, lambda path: None)[0]["verdict"] == "unknown"


def test_it_checks_once_an_hour(config):
    cfg = replace(config, obp_base_url="http://obp")
    _, asked, collector = check(cfg, lambda path: 401)
    n = len(asked)
    assert collector.check_secured() == "secured unchanged" and len(asked) == n


def test_it_checks_again_on_the_next_poll_when_it_could_not_tell(config):
    cfg = replace(config, obp_base_url="http://obp")
    _, asked, collector = check(cfg, lambda path: None)
    n = len(asked)
    assert collector.check_secured() == "endpoints unknown without a token" and len(asked) == 2 * n
