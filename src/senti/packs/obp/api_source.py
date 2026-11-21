"""The `obp` source: the few OBP-API calls Senti makes, as a Platform App: the log cache, Telemetry, aggregate
metrics, the resource docs and reachable Roles, and its Scope declaration. Plus the public root endpoint, for the git
commit the instance runs. An OBP app is read through the `obp-app` source (app_source.py), which uses the same token.

Senti calls OBP as its own application (OAuth2 client credentials from the OIDC provider), not as a User.
"""

import logging
import time
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from urllib.parse import quote

from typing import TYPE_CHECKING

import httpx

from ...source import REACH_FORMAT
from ...sources import COMMIT, LOGS, METRICS, REACH, TELEMETRY, Source
from ...store import now

if TYPE_CHECKING:
    from ...config import Config
    from ...store import Store

logger = logging.getLogger(__name__)

PLATFORM_APP_API_VERSION = "v7.0.0"
# OBP-API's error when a declared Scope names a Role it does not know (among other checks)
UNKNOWN_ROLE_DECLARED = "OBP-35048"
# Renew the token this many seconds before it expires
TOKEN_MARGIN_SECONDS = 30
# Once OBP has accepted the Platform App declaration with every Scope held, declare again this often (an admin may
# have re-marked it)
REDECLARE_EVERY_SECONDS = 3600
# In a ResourceDoc's errors, these mean the caller must be logged in or an identified application
CREDENTIALS_ERRORS = ("OBP-20001", "OBP-20200")


class OBPError(Exception):
    pass


def required_scopes(config: "Config") -> list[dict]:
    """The Scopes Senti needs, in the form PUT /consumers/current/platform-app takes."""
    scopes = [
        {
            "role_name": f"CanGetSystemLogCache{level.capitalize()}",
            "bank_id": "",
            "needed_for": f"Counting {level} log lines to find what goes wrong most often",
            "optional": False,
        }
        for level in config.log_levels
    ]
    scopes.append({
        "role_name": "CanGetTelemetry",
        "bank_id": "",
        "needed_for": "Comparing request rates and latencies between hours to find what is getting slower",
        "optional": False,
    })
    scopes.append({
        "role_name": "CanReadAggregateMetrics",
        "bank_id": "",
        "needed_for": "Counting API calls and their response times per time bucket, to compare usage and speed between hours",
        "optional": False,
    })
    # Optional: OBP-API instances older than the reachable-roles endpoint do not know this Role
    scopes.append({
        "role_name": "CanGetReachableRoles",
        "bank_id": "",
        "needed_for": "Learning which Roles anyone holds (names only), to review first the source code that can be reached",
        "optional": True,
    })
    return scopes


def _senti_version() -> str | None:
    try:
        return version("senti")
    except PackageNotFoundError:
        return None


def _obp_date(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class ObpSource(Source):
    """Every error names the full URL it was calling: Senti may watch several instances."""

    name = "obp"
    offers = frozenset({LOGS, TELEMETRY, METRICS, COMMIT, REACH})

    def __init__(self, config: "Config", http: httpx.Client | None = None):
        super().__init__(config)
        self.http = http or httpx.Client(timeout=30)
        self.declared_at = 0.0
        self.consumer_id: str | None = None
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._token_endpoint: str | None = None

    def _send(self, method: str, url: str, **kwargs) -> httpx.Response:
        try:
            return self.http.request(method, url, **kwargs)
        except httpx.HTTPError as e:
            raise OBPError(f"{method} {url} failed: {e}") from e

    def _discover_token_endpoint(self) -> str:
        if not self._token_endpoint:
            url = f"{self.config.oidc_issuer}/.well-known/openid-configuration"
            response = self._send("GET", url)
            if response.status_code != 200:
                raise OBPError(f"OIDC discovery at {url} failed ({response.status_code}): {response.text[:300]}")
            self._token_endpoint = response.json()["token_endpoint"]
        return self._token_endpoint

    def _fetch_token(self) -> str:
        c = self.config
        if not (c.oidc_issuer and c.oidc_client_id and c.oidc_client_secret):
            p = c.env_prefix
            raise OBPError(f"{p}OIDC_ISSUER, {p}OIDC_CLIENT_ID and {p}OIDC_CLIENT_SECRET must be set")
        token_url = self._discover_token_endpoint()
        response = self._send(
            "POST", token_url,
            data={"grant_type": "client_credentials", "scope": "openid"},
            auth=(c.oidc_client_id, c.oidc_client_secret),
        )
        if response.status_code != 200:
            raise OBPError(f"Client credentials token request to {token_url} failed ({response.status_code}): "
                           f"{response.text[:300]}")
        body = response.json()
        self._token = body["access_token"]
        self._token_expires_at = time.monotonic() + float(body.get("expires_in", 300)) - TOKEN_MARGIN_SECONDS
        logger.info("Got an application token")
        return self._token

    def _token_now(self) -> str:
        if self._token and time.monotonic() < self._token_expires_at:
            return self._token
        return self._fetch_token()

    def _request(self, method: str, version: str, path: str, params: dict | None = None, json: dict | None = None) -> dict | list:
        return self._request_url(method, f"{self.config.obp_base_url}/obp/{version}/{path}", params, json)

    def _request_url(self, method: str, url: str, params: dict | None = None, json: dict | None = None) -> dict | list:
        for attempt in range(2):
            headers = {"Authorization": f"Bearer {self._token_now()}"}
            response = self._send(method, url, params=params, json=json, headers=headers)
            if response.status_code == 401 and attempt == 0:
                self._token = None  # token expired or revoked: get a new one once
                continue
            if response.status_code != 200:
                raise OBPError(f"{method} {url} failed ({response.status_code}): {response.text[:300]}")
            return response.json()
        raise OBPError(f"{method} {url} failed after getting a new token")

    def status_without_token(self, url: str) -> int | None:
        """The HTTP status of a GET sent with no token, or None if there was no answer."""
        try:
            return self.http.get(url).status_code
        except httpx.HTTPError:
            return None

    def root(self) -> dict:
        """The instance's API info, with the git_commit it runs. Public: no token needed."""
        url = f"{self.config.obp_base_url}/obp/{self.config.root_api_version}/root"
        response = self._send("GET", url)
        if response.status_code != 200:
            raise OBPError(f"GET {url} failed ({response.status_code}): {response.text[:300]}")
        return response.json()

    def log_cache(self, level: str, limit: int) -> list[str]:
        """Raw log messages of one level, newest first."""
        body = self._request("GET", self.config.log_cache_api_version, f"system/log-cache/{level}", {"limit": limit})
        return [entry["message"] for entry in body.get("entries", [])]

    def telemetry(self) -> dict:
        return self._request("GET", self.config.telemetry_api_version, "management/telemetry")

    def aggregate_metrics(self, from_ts: int, to_ts: int) -> dict:
        """Call count, response times (ms) and distinct users/consumers/consents for calls in [from_ts, to_ts)."""
        params = {"from_date": _obp_date(from_ts), "to_date": _obp_date(to_ts)}
        body = self._request("GET", self.config.aggregate_metrics_api_version, "management/aggregate-metrics", params)
        # Versions before v6.0.0 return a one-element list
        return (body[0] if body else {}) if isinstance(body, list) else body

    def api_versions(self) -> list[dict]:
        """The API versions this instance has (OBP, Berlin Group, UK ...), each active or not."""
        return self._request("GET", "v6.0.0", "api/versions")["scanned_api_versions"]

    def resource_docs(self, fully_qualified_version: str) -> list[dict]:
        """The static endpoints one API version serves, each with its operation_id and the Roles it needs."""
        body = self._request("GET", PLATFORM_APP_API_VERSION, f"resource-docs/{fully_qualified_version}/obp",
                             {"content": "static"})
        return body.get("resource_docs", [])

    def reachable_roles(self) -> list[str]:
        """The names of the Roles anyone holds, as an Entitlement or a Scope. Nothing about who holds them."""
        return self._request("GET", PLATFORM_APP_API_VERSION, "reachable-roles")["role_names"]

    def current_consumer_id(self) -> str:
        """Senti's own consumer_id. Needs no Role or Scope, so OBP-API answers it before the Consumer is a Platform App.
        For an app, its OBP-API instance answers: the app only passes the token on."""
        url = f"{self.config.obp_api_url}/obp/{PLATFORM_APP_API_VERSION}/consumers/current/identity"
        return self._request_url("GET", url)["consumer_id"]

    def my_role(self, role: str) -> dict:
        """What OBP-API says about this token and a system Role: whether its User or its Consumer holds it, and whether
        an endpoint needing it would let the token in. The question an app asks before serving its monitoring endpoints."""
        url = f"{self.config.obp_api_url}/obp/{PLATFORM_APP_API_VERSION}/banks/SYS/my/roles/{quote(role, safe='')}"
        return self._request_url("GET", url)

    def declare_platform_app(self) -> dict:
        """Tell OBP-API which Scopes Senti needs. Returns the app as OBP sees it, each Scope held or not."""
        scopes = required_scopes(self.config)
        try:
            return self._request("PUT", PLATFORM_APP_API_VERSION, "consumers/current/platform-app",
                                 json={"version": _senti_version(), "required_scopes": scopes})
        except OBPError as e:
            if UNKNOWN_ROLE_DECLARED not in str(e):
                raise
        # An older OBP-API that does not know an optional Role refuses the whole declaration: declare without them
        logger.info("OBP-API refused an optional Scope; declaring the required ones only")
        return self._request("PUT", PLATFORM_APP_API_VERSION, "consumers/current/platform-app",
                             json={"version": _senti_version(),
                                   "required_scopes": [s for s in scopes if not s["optional"]]})

    # --- what every source offers (sources/__init__.py) ---------------------------------------

    def logs(self, level: str, limit: int) -> list[str]:
        return self.log_cache(level, limit)

    def metrics(self, from_ts: int, to_ts: int) -> dict:
        return self.aggregate_metrics(from_ts, to_ts)

    def running_commit(self) -> str | None:
        return self.root().get("git_commit")

    def reach(self, git_commit: str | None) -> dict:
        """Every endpoint the instance serves with the Roles it needs and whether it needs a login, and the Roles
        anyone holds (None if unknown)."""
        endpoints: dict[str, dict] = {}
        versions = [v["fully_qualified_version"] for v in self.api_versions()
                    if v.get("is_active") and not v["api_short_version"].startswith("dynamic")]
        for version in versions:
            for doc in self.resource_docs(version):
                errors = " ".join(str(e) for e in doc.get("error_response_bodies") or [])
                endpoints[doc["operation_id"]] = {"roles": sorted({r["role"] for r in doc.get("roles") or []}),
                                                  "login": any(code in errors for code in CREDENTIALS_ERRORS)}
        try:
            roles = self.reachable_roles()
        except Exception:
            roles = None  # an instance without the endpoint, or without the Scope
        return {"format": REACH_FORMAT, "ts": now(), "git_commit": git_commit, "versions": versions,
                "endpoints": endpoints, "roles": roles}

    def secured_paths(self) -> list[str]:
        """OBP-API's log cache, Telemetry and aggregate metrics."""
        c = self.config
        return ([f"/obp/{c.log_cache_api_version}/system/log-cache/{level}" for level in [*c.log_levels, "all"]]
                + [f"/obp/{c.telemetry_api_version}/management/telemetry",
                   f"/obp/{c.aggregate_metrics_api_version}/management/aggregate-metrics"])

    def before_poll(self, store: "Store") -> None:
        """Tell OBP-API which Scopes Senti needs, and say which are missing.

        Refused until an administrator marks Senti's Consumer as a Platform App, so this is retried on every poll until
        accepted: marking takes effect without a restart. Polling goes ahead either way.
        """
        if self.declared_at and time.monotonic() - self.declared_at < REDECLARE_EVERY_SECONDS:
            return
        try:
            app = self.declare_platform_app()
        except Exception as e:
            logger.warning("Could not declare Senti's Scopes (is its Consumer marked as a Platform App?): %s", e)
            state = {"ts": now(), "ok": False, "error": str(e)[:500]}
            # So the operator is told which Consumer to mark by its consumer_id, not by the client id (its key)
            if consumer_id := self.known_consumer_id():
                state["consumer_id"] = consumer_id
            store.set_state("platform_app", state)
            return
        missing = [s["role_name"] for s in app.get("required_scopes", []) if not s.get("held") and not s.get("optional")]
        # While a Scope is missing, ask again on every poll, so a grant shows within a poll, as the health page says
        self.declared_at = 0.0 if missing else time.monotonic()
        store.set_state("platform_app", {"ts": now(), "ok": True, "consumer_id": app.get("consumer_id"),
                                         "label": app.get("label"), "missing": missing})
        if missing:
            logger.warning("Consumer %s lacks the Scopes %s; ask an administrator to grant them",
                           app.get("consumer_id"), ", ".join(missing))
        else:
            logger.info("Declared as Platform App %r; all Scopes held", app.get("label"))

    def known_consumer_id(self) -> str | None:
        """Senti's consumer_id, asked once; None while OBP-API cannot say (unreachable, token rejected, older version)."""
        if not self.consumer_id:
            try:
                self.consumer_id = self.current_consumer_id()
            except Exception as e:
                logger.info("Could not look up Senti's consumer_id: %s", e)
                return None
        return self.consumer_id
