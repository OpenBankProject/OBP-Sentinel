"""The OBP apps an OBP-API instance lists in its public app directory (GET /obp/v6.0.0/app-directory), each read
through the `obp-app` source. Kept in the instance's database, which answers when OBP-API cannot be reached.

Nothing here imports the configuration: config.py builds each app's instance from this list.
"""

import logging
from typing import TYPE_CHECKING, NamedTuple

import httpx

if TYPE_CHECKING:
    from ...config import Config

logger = logging.getLogger(__name__)

APP_DIRECTORY_STATE = "app_directory"


class App(NamedTuple):
    directory_name: str  # its entry in OBP-API's app directory
    monitoring_path: str  # where the app serves its log-cache and telemetry endpoints
    source_setting: str  # the setting naming its checkout
    github_repo: str
    service: str  # its service's name in senti.toml
    kind: str
    folder: str = ""  # where its code is in its checkout, if not at the top


APPS = {
    "explorer": App("public_obp_api_explorer_url", "/api/monitoring", "EXPLORER_SOURCE", "OpenBankProject/API-Explorer-II",
                    "api-explorer", "vue-express"),
    "manager": App("public_obp_api_manager_url", "/monitoring", "FRONTEND_SOURCE", "OpenBankProject/OBP-Frontend",
                   "api-manager", "sveltekit", "apps/api-manager"),
    "portal": App("public_obp_portal_url", "/monitoring", "FRONTEND_SOURCE", "OpenBankProject/OBP-Frontend",
                  "portal", "sveltekit", "apps/portal"),
    "cohort": App("public_obp_cohort_url", "/monitoring", "FRONTEND_SOURCE", "OpenBankProject/OBP-Frontend",
                  "cohort", "sveltekit", "apps/cohort"),
    "oidc": App("public_obp_oidc_url", "/monitoring", "OIDC_SOURCE", "OpenBankProject/OBP-OIDC",
                "obp-oidc", "scala-http4s"),
    "populator": App("public_obp_sandbox_populator_url", "/monitoring", "FRONTEND_SOURCE",
                     "OpenBankProject/OBP-Frontend", "sandbox-populator", "sveltekit", "apps/sandbox-populator"),
}


def kept_directory(config: "Config", urls: dict[str, str] | None = None) -> dict[str, str]:
    """Keep an instance's apps in its database (if `urls`), or read them from it. Nothing if it cannot be opened."""
    from ...store import Store

    try:
        store = Store(config.db_path)
    except Exception as e:
        logger.warning("Cannot open %s to keep the apps of %s: %s", config.db_path, config.name, e)
        return urls or {}
    try:
        if urls is None:
            return store.get_state(APP_DIRECTORY_STATE, {})
        store.set_state(APP_DIRECTORY_STATE, urls)
        store.commit()
        return urls
    finally:
        store.close()


def read_app_directory(config: "Config") -> dict[str, str]:
    """The URLs of an OBP-API instance's apps, by app, from its public app directory, and kept; the ones kept before
    if it cannot be read."""
    try:
        response = httpx.get(f"{config.obp_base_url}/obp/v6.0.0/app-directory", timeout=10)
        response.raise_for_status()
        listed = {entry["name"]: entry["value"] for entry in response.json().get("app_directory", [])}
        return kept_directory(config, {app: listed[a.directory_name].rstrip("/")
                                       for app, a in APPS.items() if listed.get(a.directory_name)})
    except Exception as e:
        urls = kept_directory(config)
        logger.warning("Could not read the app directory of %s (%s); using the apps found before: %s",
                       config.name, e, ", ".join(urls) or "none")
        return urls
