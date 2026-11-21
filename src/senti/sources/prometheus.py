"""The `prometheus` source: an instance's Telemetry, metrics and running commit, from Prometheus's HTTP query API
(GET <url>/api/v1/query). The instance's series are those matching its selector in senti.toml, label matchers such
as job="obp-api",instance="localhost:9464".

Telemetry: every series the selector matches, now, turned into meters in the shape OBP-API serves them, so the rest of
Senti reads both alike. Prometheus's names are Micrometer's with dots as underscores and the unit added, so they are
turned back: obp_api_endpoint_requests_seconds_count and _sum make the timer obp.api.endpoint.requests (count,
total_time in seconds), http_server_requests_seconds_max its max, a name ending _total a counter (count), anything
else a gauge (value). Histogram buckets are left out.

Metrics: for each bucket, the calls and their mean and longest response time, from the standard HTTP server timer
(http_server_requests_seconds, or the one named by the instance's prometheus_requests). Distinct users and consumers
are OBP-API's own record: Prometheus has none.

The running commit: a label on an info series (target_info, *_build_info, *_info): git_commit, vcs_ref_head_revision,
service_version or commit, whichever comes first.
"""

import os
import re
from typing import TYPE_CHECKING

import httpx

from . import COMMIT, METRICS, TELEMETRY, Source

if TYPE_CHECKING:
    from ..config import Config

REQUESTS = "http_server_requests_seconds"  # Micrometer's and OpenTelemetry's HTTP server timer, in Prometheus
COMMIT_LABELS = ("git_commit", "vcs_ref_head_revision", "service_version", "commit")
UNITS = re.compile(r"_(seconds|bytes)$")
DROPPED = {"__name__", "job", "instance"}  # which target a series came from, not what it measures


class PrometheusError(Exception):
    pass


def meters_of(series: list[dict]) -> list[dict]:
    """Prometheus series (an instant query's result) as meters: {name, type, tags, measurements}."""
    meters: dict[tuple, dict] = {}
    for s in series:
        name, labels = s["metric"].get("__name__", ""), s["metric"]
        if not name or name.endswith("_bucket"):
            continue
        try:
            value = float(s["value"][1])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        tags = {k: v for k, v in sorted(labels.items()) if k not in DROPPED}
        for suffix, mtype, stat in (("_count", "timer", "count"), ("_sum", "timer", "total_time"),
                                    ("_max", "timer", "max"), ("_total", "counter", "count")):
            if name.endswith(suffix):
                base = name[: -len(suffix)]
                break
        else:
            base, mtype, stat = name, "gauge", "value"
        dotted = UNITS.sub("", base).replace("_", ".")
        meter = meters.setdefault((dotted, tuple(tags.items())),
                                  {"name": dotted, "type": mtype, "tags": tags, "measurements": {}})
        if mtype == "timer":
            meter["type"] = "timer"  # a _max seen before its _count still makes a timer
        meter["measurements"][stat] = value
    # A _count or _sum alone (a summary without the other) is a counter, not a timer
    for m in meters.values():
        if m["type"] == "timer" and set(m["measurements"]) == {"count"}:
            m["type"] = "counter"
    return list(meters.values())


class PrometheusSource(Source):
    name = "prometheus"
    offers = frozenset({TELEMETRY, METRICS, COMMIT})

    def __init__(self, config: "Config", http: httpx.Client | None = None):
        super().__init__(config)
        self.http = http or httpx.Client(timeout=30)
        self.selector = config.prometheus_selector.strip().strip("{}")
        self.requests = os.environ.get(f"{config.env_prefix}PROMETHEUS_REQUESTS", REQUESTS)

    def query(self, promql: str, at: int | None = None) -> list[dict]:
        """An instant query's result. Every error names the server and the query, as Senti may watch several."""
        url = f"{self.config.prometheus_url}/api/v1/query"
        params = {"query": promql, **({"time": str(at)} if at is not None else {})}
        headers = {"Authorization": f"Bearer {token}"} if (token := os.environ.get("PROMETHEUS_TOKEN")) else {}
        try:
            response = self.http.get(url, params=params, headers=headers)
        except httpx.HTTPError as e:
            raise PrometheusError(f"GET {url} ({promql}) failed: {e}") from e
        if response.status_code != 200:
            raise PrometheusError(f"GET {url} ({promql}) failed ({response.status_code}): {response.text[:300]}")
        body = response.json()
        if body.get("status") != "success":
            raise PrometheusError(f"GET {url} ({promql}) failed: {body.get('error', body)}")
        return body["data"]["result"]

    def _scalar(self, promql: str, at: int) -> float | None:
        result = self.query(promql, at)
        try:
            value = float(result[0]["value"][1])
        except (IndexError, KeyError, TypeError, ValueError):
            return None
        return None if value != value else value  # NaN: no calls, so no mean

    def telemetry(self) -> dict:
        if not self.selector:
            raise PrometheusError(f"{self.config.name} has no prometheus selector in senti.toml")
        return {"meters": meters_of(self.query("{" + self.selector + "}")), "git_commit": self.running_commit()}

    def metrics(self, from_ts: int, to_ts: int) -> dict:
        """Calls in [from_ts, to_ts) and their mean and longest response time (ms), as OBP-API's aggregate metrics
        name them."""
        window, sel, req = f"{max(1, to_ts - from_ts)}s", self.selector, self.requests
        count = self._scalar(f"sum(increase({req}_count{{{sel}}}[{window}]))", to_ts) or 0
        total = self._scalar(f"sum(increase({req}_sum{{{sel}}}[{window}]))", to_ts)
        longest = self._scalar(f"max(max_over_time({req}_max{{{sel}}}[{window}]))", to_ts)
        return {"count": round(count),
                "average_response_time": round(1000 * total / count, 1) if count and total is not None else None,
                "maximum_response_time": round(1000 * longest, 1) if longest is not None else None}

    def running_commit(self) -> str | None:
        sel = self.selector
        info = self.query('{__name__=~"target_info|.+_build_info|.+_info"' + (f",{sel}" if sel else "") + "}")
        for label in COMMIT_LABELS:
            for s in info:
                if value := s["metric"].get(label):
                    return value
        return None
