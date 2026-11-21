"""The `obp-app` source: an OBP app (API Explorer II, the API Manager, the Portal, Cohort, OBP-OIDC, the Sandbox
Populator) serving its own log cache and Telemetry at its monitoring path, in OBP-API's shape. The app checks Senti's
token and Roles with OBP-API, so Senti uses its OBP-API instance's token. The Telemetry says which commit the app
runs. There are no aggregate metrics, root endpoint or resource docs, and the Scopes are declared by watching OBP-API.
"""

import logging
import re
from typing import TYPE_CHECKING

from ...sources import LOGS, TELEMETRY
from ...store import now
from .api_source import ObpSource

if TYPE_CHECKING:
    from ...store import Store

logger = logging.getLogger(__name__)

# What OBP-API says about the Roles an app refused Senti, and Senti's consumer_id (health.py reads it)
APP_ACCESS = "app_access"
# An app's 403: GET <app>/monitoring/... failed (403): {"error":"Requires one of the Roles: A, B"}
APP_REFUSED = re.compile(r"failed \(403\): .*Requires one of the Roles: (?P<roles>[\w, ]+)")


class ObpAppSource(ObpSource):
    name = "obp-app"
    offers = frozenset({LOGS, TELEMETRY})

    def _monitoring(self, path: str) -> str:
        return f"{self.config.obp_base_url}{self.config.monitoring_path}/{path}"

    def log_cache(self, level: str, limit: int) -> list[str]:
        body = self._request_url("GET", self._monitoring(f"log-cache/{level}"), {"limit": limit})
        return [entry["message"] for entry in body.get("entries", [])]

    def telemetry(self) -> dict:
        return self._request_url("GET", self._monitoring("telemetry"))

    def secured_paths(self) -> list[str]:
        """The app's /monitoring log cache and Telemetry."""
        c = self.config
        return [f"{c.monitoring_path}/log-cache/{level}" for level in [*c.log_levels, "all"]] + [
            f"{c.monitoring_path}/telemetry"]

    def before_poll(self, store: "Store") -> None:
        """Nothing: Senti's Scopes are declared by the source watching the app's OBP-API instance."""

    def after_poll(self, store: "Store") -> str:
        """When the app refuses Senti's token (403), ask OBP-API what the app asks it: which Consumer the token is,
        and whether it may use each Role. So the health page can say which Consumer lacks which Scope, or that the
        app refuses Roles OBP-API says Senti holds (the app and OBP-API disagree)."""
        errors = [r[0] for r in store.db.execute(
            """SELECT p.error FROM polls p JOIN (SELECT source, MAX(id) AS id FROM polls GROUP BY source) l ON l.id = p.id
               WHERE NOT p.ok AND p.error IS NOT NULL""")]
        refused = sorted({role.strip() for e in errors if (m := APP_REFUSED.search(e)) for role in m["roles"].split(",")
                          if role.strip()})
        if not refused:
            store.set_state(APP_ACCESS, {"ts": now(), "roles": {}})
            return "access ok"
        roles = {}
        for role in refused:
            try:
                answer = self.my_role(role)
                held = answer.get("held_by") or {}
                roles[role] = {"consumer": bool(held.get("consumer")), "user": bool(held.get("user")),
                               "allowed": answer.get("allowed") is True}
            except Exception as e:  # an OBP-API before v7.0.0 /my/roles, or unreachable: the health page says less
                logger.info("Could not ask OBP-API about %s: %s", role, e)
                roles[role] = {"error": str(e)[:300]}
        store.set_state(APP_ACCESS, {"ts": now(), "consumer_id": self.known_consumer_id(), "roles": roles})
        held = [r for r, a in roles.items() if a.get("consumer") or a.get("user")]
        return f"refused {len(refused)} Roles" + (f", {len(held)} of them held" if held else "")
