"""The few OBP-API calls Sentinel makes, as a Platform App: the log cache, Telemetry, aggregate metrics and its
Scope declaration.

Sentinel calls OBP as its own application (OAuth2 client credentials from OBP-OIDC), not as a User.
"""

import logging
import time
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version

import httpx

from .config import Config

logger = logging.getLogger(__name__)

PLATFORM_APP_API_VERSION = "v7.0.0"
# Renew the token this many seconds before it expires
TOKEN_MARGIN_SECONDS = 30


class OBPError(Exception):
    pass


def required_scopes(config: Config) -> list[dict]:
    """The Scopes Sentinel needs, in the form PUT /consumers/current/platform-app takes."""
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
    return scopes


def _sentinel_version() -> str | None:
    try:
        return version("obp-sentinel")
    except PackageNotFoundError:
        return None


def _obp_date(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class OBPClient:
    def __init__(self, config: Config, http: httpx.Client | None = None):
        self.config = config
        self.http = http or httpx.Client(timeout=30)
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._token_endpoint: str | None = None

    def _discover_token_endpoint(self) -> str:
        if not self._token_endpoint:
            url = f"{self.config.oidc_issuer}/.well-known/openid-configuration"
            response = self.http.get(url)
            if response.status_code != 200:
                raise OBPError(f"OIDC discovery at {url} failed ({response.status_code}): {response.text[:300]}")
            self._token_endpoint = response.json()["token_endpoint"]
        return self._token_endpoint

    def _fetch_token(self) -> str:
        c = self.config
        if not (c.oidc_issuer and c.oidc_client_id and c.oidc_client_secret):
            raise OBPError("OIDC_ISSUER, OIDC_CLIENT_ID and OIDC_CLIENT_SECRET must be set")
        response = self.http.post(
            self._discover_token_endpoint(),
            data={"grant_type": "client_credentials", "scope": "openid"},
            auth=(c.oidc_client_id, c.oidc_client_secret),
        )
        if response.status_code != 200:
            raise OBPError(f"Client credentials token request failed ({response.status_code}): {response.text[:300]}")
        body = response.json()
        self._token = body["access_token"]
        self._token_expires_at = time.monotonic() + float(body.get("expires_in", 300)) - TOKEN_MARGIN_SECONDS
        logger.info("Got an application token for client %s", c.oidc_client_id)
        return self._token

    def _token_now(self) -> str:
        if self._token and time.monotonic() < self._token_expires_at:
            return self._token
        return self._fetch_token()

    def _request(self, method: str, version: str, path: str, params: dict | None = None, json: dict | None = None) -> dict | list:
        url = f"{self.config.obp_base_url}/obp/{version}/{path}"
        for attempt in range(2):
            headers = {"Authorization": f"Bearer {self._token_now()}"}
            response = self.http.request(method, url, params=params, json=json, headers=headers)
            if response.status_code == 401 and attempt == 0:
                self._token = None  # token expired or revoked: get a new one once
                continue
            if response.status_code != 200:
                raise OBPError(f"{method} {path} failed ({response.status_code}): {response.text[:300]}")
            return response.json()
        raise OBPError(f"{method} {path} failed after getting a new token")

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

    def declare_platform_app(self) -> dict:
        """Tell OBP-API which Scopes Sentinel needs. Returns the app as OBP sees it, each Scope held or not."""
        body = {"version": _sentinel_version(), "required_scopes": required_scopes(self.config)}
        return self._request("PUT", PLATFORM_APP_API_VERSION, "consumers/current/platform-app", json=body)
