"""The few OBP-API calls Sentinel makes: DirectLogin, the log cache and Telemetry."""

import logging

import httpx

from .config import Config

logger = logging.getLogger(__name__)


class OBPError(Exception):
    pass


class OBPClient:
    def __init__(self, config: Config, http: httpx.Client | None = None):
        self.config = config
        self.http = http or httpx.Client(timeout=30)
        self._token: str | None = None

    def _login(self) -> str:
        c = self.config
        if not (c.obp_username and c.obp_password and c.obp_consumer_key):
            raise OBPError("OBP_USERNAME, OBP_PASSWORD and OBP_CONSUMER_KEY must be set")
        response = self.http.post(
            f"{c.obp_base_url}/my/logins/direct",
            headers={
                "Content-Type": "application/json",
                "directlogin": f"username={c.obp_username},password={c.obp_password},consumer_key={c.obp_consumer_key}",
            },
        )
        if response.status_code != 201:
            raise OBPError(f"DirectLogin failed ({response.status_code}): {response.text[:300]}")
        self._token = response.json()["token"]
        logger.info("Logged in to %s as %s", c.obp_base_url, c.obp_username)
        return self._token

    def _get(self, version: str, path: str, params: dict | None = None) -> dict:
        url = f"{self.config.obp_base_url}/obp/{version}/{path}"
        for attempt in range(2):
            token = self._token or self._login()
            response = self.http.get(url, params=params, headers={"Authorization": f"DirectLogin token={token}"})
            if response.status_code == 401 and attempt == 0:
                self._token = None  # token expired: log in again once
                continue
            if response.status_code != 200:
                raise OBPError(f"GET {path} failed ({response.status_code}): {response.text[:300]}")
            return response.json()
        raise OBPError(f"GET {path} failed after logging in again")

    def log_cache(self, level: str, limit: int) -> list[str]:
        """Raw log messages of one level, newest first."""
        body = self._get(self.config.log_cache_api_version, f"system/log-cache/{level}", {"limit": limit})
        return [entry["message"] for entry in body.get("entries", [])]

    def telemetry(self) -> dict:
        return self._get(self.config.telemetry_api_version, "management/telemetry")
