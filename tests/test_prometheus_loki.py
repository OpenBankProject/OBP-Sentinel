"""Prometheus and Loki as sources: any service's Telemetry, metrics, running commit and logs, through their query
APIs. The answers below are shaped as Prometheus 2/3 and Loki 2/3 answer."""

import tomllib
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from senti.collector import Collector
from senti.config import Config, ConfigError
from senti.model import SetupError, parse
from senti.sources import Combined, offers_of, source_for
from senti.sources.loki import LokiSource
from senti.sources.prometheus import PrometheusSource, meters_of
from senti.store import Store


def series(name, value, **labels):
    return {"metric": {"__name__": name, "job": "obp-api", "instance": "localhost:9464", **labels},
            "value": [1795245000.0, str(value)]}


def prometheus(config, answer):
    """A PrometheusSource whose server answers each query with `answer(query, time)`; and the queries asked."""
    asked = []

    def handler(request):
        params = {k: v[0] for k, v in parse_qs(urlsplit(str(request.url)).query).items()}
        asked.append(params)
        assert request.url.path == "/api/v1/query"
        return httpx.Response(200, json={"status": "success",
                                         "data": {"resultType": "vector",
                                                  "result": answer(params["query"], params.get("time"))}})

    cfg = replace(config, sources=("prometheus",), prometheus_url="http://prom:9090",
                  prometheus_selector='job="obp-api",instance="localhost:9464"')
    return PrometheusSource(cfg, httpx.Client(transport=httpx.MockTransport(handler))), asked


def test_series_become_meters_as_micrometer_names_them():
    meters = {(m["name"], tuple(m["tags"].items())): m for m in meters_of([
        series("obp_api_endpoint_requests_seconds_count", 3, operation="getBanks", status="2xx"),
        series("obp_api_endpoint_requests_seconds_sum", 0.3, operation="getBanks", status="2xx"),
        series("obp_api_endpoint_requests_seconds_bucket", 3, operation="getBanks", status="2xx", le="0.5"),
        series("http_server_requests_seconds_max", 0.2, uri="/banks"),
        series("obp_api_cache_gets_total", 7, cache="banks"),
        series("jvm_memory_used_bytes", 1024, area="heap"),
        series("hikaricp_connections_active", 2, pool="main"),
    ])}
    timer = meters[("obp.api.endpoint.requests", (("operation", "getBanks"), ("status", "2xx")))]
    assert timer["type"] == "timer" and timer["measurements"] == {"count": 3.0, "total_time": 0.3}
    assert meters[("http.server.requests", (("uri", "/banks"),))]["measurements"] == {"max": 0.2}
    assert meters[("obp.api.cache.gets", (("cache", "banks"),))] ["type"] == "counter"
    assert meters[("jvm.memory.used", (("area", "heap"),))]["measurements"] == {"value": 1024.0}
    assert meters[("hikaricp.connections.active", (("pool", "main"),))]["type"] == "gauge"
    assert not any("bucket" in name for name, _ in meters)  # histogram buckets are left out


def test_telemetry_reads_the_instances_series_and_its_commit(config):
    def answer(query, at):
        if query.startswith('{__name__=~"target_info'):
            return [series("target_info", 1, service_name="obp-api", vcs_ref_head_revision="abc1234")]
        return [series("jvm_threads_live_threads", 40)]

    source, asked = prometheus(config, answer)
    body = source.telemetry()
    assert body["git_commit"] == "abc1234" and body["meters"][0]["name"] == "jvm.threads.live.threads"
    assert asked[0]["query"] == '{job="obp-api",instance="localhost:9464"}'


def test_metrics_are_calls_and_response_times_per_bucket(config):
    values = {"sum(increase(http_server_requests_seconds_count": 120, "sum(increase(http_server_requests_seconds_sum": 6,
              "max(max_over_time(http_server_requests_seconds_max": 0.9}
    source, asked = prometheus(config, lambda q, at: [{"metric": {}, "value": [at, str(next(
        v for k, v in values.items() if q.startswith(k)))]}])
    assert source.metrics(1795244100, 1795245000) == {"count": 120, "average_response_time": 50.0,
                                                       "maximum_response_time": 900.0}
    assert asked[0]["query"].endswith('[900s]))') and asked[0]["time"] == "1795245000"


def test_no_calls_means_no_mean(config):
    source, _ = prometheus(config, lambda q, at: [] if "count" in q else [{"metric": {}, "value": [at, "NaN"]}])
    assert source.metrics(0, 900) == {"count": 0, "average_response_time": None, "maximum_response_time": None}


def test_a_failing_query_says_which_server_and_query(config):
    cfg = replace(config, prometheus_url="http://prom:9090", prometheus_selector='job="x"')
    source = PrometheusSource(cfg, httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(400, json={"status": "error", "error": "parse error"}))))
    with pytest.raises(Exception, match=r"http://prom:9090/api/v1/query \(\{job=\"x\"\}\) failed \(400\)"):
        source.telemetry()


def loki(config, streams):
    asked = []

    def handler(request):
        asked.append({k: v[0] for k, v in parse_qs(urlsplit(str(request.url)).query).items()})
        assert request.url.path == "/loki/api/v1/query_range"
        return httpx.Response(200, json={"status": "success", "data": {"resultType": "streams", "result": streams}})

    cfg = replace(config, sources=("loki",), loki_url="http://loki:3100", loki_selector='{app="shop",env="local"}')
    return LokiSource(cfg, httpx.Client(transport=httpx.MockTransport(handler))), asked


def test_loki_gives_a_levels_newest_lines_first_across_streams(config):
    source, asked = loki(config, [
        {"stream": {"app": "shop", "pod": "a"}, "values": [["3000", "ERROR c"], ["1000", "ERROR a"]]},
        {"stream": {"app": "shop", "pod": "b"}, "values": [["2000", "ERROR b"]]},
    ])
    assert source.logs("error", 2) == ["ERROR c", "ERROR b"]
    q = asked[0]
    assert q["query"] == '{app="shop",env="local"} |~ "(?i)\\\\b(error|fatal|severe|critical)\\\\b"'
    assert (q["direction"], q["limit"]) == ("backward", "2")


def test_obp_api_read_through_obp_and_prometheus_takes_each_from_the_first_that_offers_it(config):
    cfg = replace(config, sources=("obp", "prometheus"), prometheus_url="http://prom:9090",
                  prometheus_selector='job="obp-api"')
    source = source_for(cfg)
    assert isinstance(source, Combined) and source.name == "obp+prometheus"
    assert source._for("telemetry").name == "obp" and source._for("metrics").name == "obp"
    assert offers_of(replace(cfg, sources=("loki",))) == {"logs"}


def test_a_service_read_only_from_prometheus_and_loki_is_watched(config, tmp_path):
    class Prom(PrometheusSource):
        def query(self, promql, at=None):
            if "target_info" in promql:
                return []
            if promql.startswith("sum(increase(http_server_requests_seconds_count"):
                return [{"metric": {}, "value": [at, "10"]}]
            if promql.startswith("{"):
                return [series("http_server_requests_seconds_count", 10, uri="/cart", status="500")]
            return []

    class Logs(LokiSource):
        def logs(self, level, limit):
            return ["2026-11-21 08:00:00.123 ERROR boom on /cart"] if level == "error" else []

    cfg = replace(config, name="shop", service="shop", kind="sveltekit", sources=("prometheus", "loki"),
                  prometheus_selector='job="shop"', loki_selector='{app="shop"}',
                  telemetry_prefixes=[], ignore_regex="", log_levels=["error"])
    store = Store(str(tmp_path / "shop.db"))
    Collector(cfg, store, Combined(cfg, [Prom(cfg), Logs(cfg)])).poll_once()
    polled = [r["source"] for r in store.db.execute("SELECT source FROM polls ORDER BY id")]
    assert polled == ["log:error", "telemetry", "metrics"]
    assert store.db.execute("SELECT COUNT(*) FROM signatures").fetchone()[0] == 1
    assert store.db.execute("SELECT COUNT(*) FROM telemetry_values").fetchone()[0] == 1


SETUP = """
[prometheus]
url = "http://prom:9090/"

[loki]
url = "http://loki:3100"

[[service]]
name = "shop"
kind = "sveltekit"
sources = ["prometheus", "loki"]

[[service]]
name = "obp-api"
kind = "obp-api"
sources = ["obp", "prometheus"]

[[environment]]
name = "production"

[[instance]]
service = "shop"
environment = "production"
url = "https://shop.example.com"
prometheus = 'job="shop"'
loki = '{app="shop"}'

[[instance]]
service = "obp-api"
environment = "production"
url = "https://api.example.com"
credentials = "PRODUCTION_OIDC"
prometheus = 'job="obp-api"'
discover = false
"""


def test_senti_toml_names_sources_servers_and_selectors(config, tmp_path, monkeypatch):
    setup = parse(tomllib.loads(SETUP))
    assert (setup.prometheus_url, setup.loki_url) == ("http://prom:9090", "http://loki:3100")
    assert setup.service("shop").sources == ("prometheus", "loki")
    (tmp_path / "senti.toml").write_text(SETUP)
    shop, obp = Config.all_from_env()
    assert (shop.kind, shop.sources, shop.prometheus_selector, shop.loki_selector) == (
        "sveltekit", ("prometheus", "loki"), 'job="shop"', '{app="shop"}')
    assert (shop.telemetry_prefixes, shop.ignore_regex) == ([], "")  # OBP-API's defaults are OBP-API's only
    assert obp.sources == ("obp", "prometheus") and obp.prometheus_url == "http://prom:9090"


@pytest.mark.parametrize("change, error", [
    (lambda t: t.replace('loki = \'{app="shop"}\'\n', ""), "its service reads loki"),
    (lambda t: t.replace('[loki]\nurl = "http://loki:3100"\n', ""), r"\[loki\] url is missing"),
    (lambda t: t.replace('sources = ["prometheus", "loki"]', ""), "a sveltekit service is read from Prometheus or Loki"),
    (lambda t: t.replace('sources = ["prometheus", "loki"]', 'sources = ["obp-app"]'), "found in their OBP-API"),
])
def test_mistakes_with_sources_are_explained(change, error):
    with pytest.raises(SetupError, match=error):
        parse(tomllib.loads(change(SETUP)))


def test_an_unknown_source_is_named(config):
    with pytest.raises(ValueError, match="reads graphite, which Senti does not know"):
        source_for(replace(config, sources=("graphite",)))
