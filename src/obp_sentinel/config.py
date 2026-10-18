"""Settings, read from the environment (and a .env file in the working directory).

Sentinel can watch several OBP-API instances: list their names in SENTINEL_INSTANCES (e.g. "local,staging").
Any setting can then be given per instance by prefixing it with the instance's name in capitals, `-` as `_`
(e.g. STAGING_OBP_BASE_URL, STAGING_OBP_API_SOURCE); without a prefixed value the plain one is used. Each
instance has its own database (sentinel-<name>.db) and digests (digests/<name>): SENTINEL_DB and
SENTINEL_DIGEST_DIR are only taken prefixed, so instances never share them. Without SENTINEL_INSTANCES
there is one instance, "default", using sentinel.db and digests/ as before.

Each OBP-API instance can have apps that serve their own log cache and Telemetry in OBP-API's shape: API
Explorer II, the API Manager, the Portal and OBP-OIDC. They are found in the instance's public app directory
(GET /obp/v6.0.0/app-directory), read at startup and kept in the instance's database for when OBP-API cannot be
reached. Each becomes an instance of its own, named <instance>-<app> (local-explorer), with its own database and
digests. It uses its OBP-API instance's settings, OIDC client included, except SENTINEL_TELEMETRY_PREFIXES and
SENTINEL_IGNORE_REGEX, which are written for OBP-API: an app takes those only prefixed with its own name
(LOCAL_EXPLORER_SENTINEL_IGNORE_REGEX). Its source is EXPLORER_SOURCE (API Explorer II), FRONTEND_SOURCE
(OBP-Frontend, for the API Manager and the Portal) or OIDC_SOURCE (OBP-OIDC), shared by all instances.
"""

import logging
import os
import re
from dataclasses import dataclass
from typing import NamedTuple

from dotenv import find_dotenv, load_dotenv


def _list(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


DEFAULT_INSTANCE = "default"
INSTANCE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")
OWN_PER_INSTANCE = {"SENTINEL_DB", "SENTINEL_DIGEST_DIR"}
# Written for OBP-API's meters and log lines: an app takes them only prefixed with its own name
OBP_API_ONLY = {"SENTINEL_TELEMETRY_PREFIXES", "SENTINEL_IGNORE_REGEX"}


class App(NamedTuple):
    directory_name: str  # its entry in OBP-API's app directory
    monitoring_path: str  # where the app serves its log-cache and telemetry endpoints
    source_setting: str  # the setting naming its checkout
    github_repo: str


APPS = {
    "explorer": App("public_obp_api_explorer_url", "/api/monitoring", "EXPLORER_SOURCE", "OpenBankProject/API-Explorer-II"),
    "manager": App("public_obp_api_manager_url", "/monitoring", "FRONTEND_SOURCE", "OpenBankProject/OBP-Frontend"),
    "portal": App("public_obp_portal_url", "/monitoring", "FRONTEND_SOURCE", "OpenBankProject/OBP-Frontend"),
    "oidc": App("public_obp_oidc_url", "/monitoring", "OIDC_SOURCE", "OpenBankProject/OBP-OIDC"),
}
APP_DIRECTORY_STATE = "app_directory"
logger = logging.getLogger(__name__)
_directories: dict[str, dict[str, str]] = {}  # by OBP-API instance: its apps' URLs, by app


def _prefix(name: str) -> str:
    return "" if name == DEFAULT_INSTANCE else name.upper().replace("-", "_") + "_"


class ConfigError(Exception):
    pass


def _obp_instance_names() -> list[str]:
    load_dotenv(find_dotenv(usecwd=True))  # the working directory's .env, not one near the code
    names = _list(os.environ.get("SENTINEL_INSTANCES", ""))
    for name in names:
        if not INSTANCE_NAME.match(name):
            raise ConfigError(f"Instance name {name!r} in SENTINEL_INSTANCES: use lower case letters, digits and -")
    if len(set(names)) != len(names):
        raise ConfigError("SENTINEL_INSTANCES names an instance twice")
    return names or [DEFAULT_INSTANCE]


def _kept_directory(config: "Config", urls: dict[str, str] | None = None) -> dict[str, str]:
    """Keep an instance's apps in its database (if `urls`), or read them from it. Nothing if it cannot be opened."""
    from .store import Store

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


def discover_apps(config: "Config") -> dict[str, str]:
    """The URLs of an OBP-API instance's apps, by app, from its public app directory. Kept in its database, which
    answers when OBP-API cannot be reached."""
    import httpx

    try:
        response = httpx.get(f"{config.obp_base_url}/obp/v6.0.0/app-directory", timeout=10)
        response.raise_for_status()
        listed = {entry["name"]: entry["value"] for entry in response.json().get("app_directory", [])}
        urls = _kept_directory(config, {app: listed[a.directory_name].rstrip("/")
                                        for app, a in APPS.items() if listed.get(a.directory_name)})
    except Exception as e:
        urls = _kept_directory(config)
        logger.warning("Could not read the app directory of %s (%s); using the apps found before: %s",
                       config.name, e, ", ".join(urls) or "none")
    _directories[config.name] = urls
    return urls


def _app_urls(parent: str) -> dict[str, str]:
    """An OBP-API instance's apps as last discovered (in this process, else as kept in its database)."""
    if parent not in _directories:
        _directories[parent] = _kept_directory(Config.from_env(parent))
    return _directories[parent]


def _app_instances(obp_names: list[str]) -> dict[str, tuple[str, str]]:
    """The apps in each OBP-API instance's app directory, by instance name: (their OBP-API instance, which app)."""
    apps = {}
    for parent in obp_names:
        for app in _app_urls(parent):
            if app in APPS:
                name = app if parent == DEFAULT_INSTANCE else f"{parent}-{app}"
                if name in obp_names:
                    raise ConfigError(f"{name} is both in SENTINEL_INSTANCES and the {app} of {parent}")
                apps[name] = (parent, app)
    return apps


def instance_names() -> list[str]:
    """The OBP-API instances, each followed by its apps."""
    obp_names = _obp_instance_names()
    apps = _app_instances(obp_names)
    return [n for parent in obp_names for n in [parent, *(a for a, (p, _) in apps.items() if p == parent)]]


@dataclass(frozen=True)
class Config:
    name: str  # the OBP-API instance (or app) this configuration watches
    obp_base_url: str  # for an app, the app's own URL
    monitoring_path: str  # "" for OBP-API; for an app, where its log-cache and telemetry endpoints are
    parent: str  # for an app, its OBP-API instance; "" for OBP-API
    oidc_issuer: str
    oidc_client_id: str
    oidc_client_secret: str
    log_cache_api_version: str
    telemetry_api_version: str
    aggregate_metrics_api_version: str
    root_api_version: str
    obp_api_source: str
    obp_api_github_repo: str  # owner/name, to date running commits missing from the checkout; "" for none

    db_path: str
    source_db_path: str  # what the source review has read, shared by all instances
    review_dir: str  # the same, as text files to keep in a private git repo
    digest_dir: str
    poll_seconds: int
    log_levels: list[str]
    fetch_limit: int
    bucket_minutes: int
    telemetry_prefixes: list[str]
    ignore_regex: str
    retention_days: int

    min_watch_hours: float
    digest_size: int
    max_per_week: int
    min_priority: float
    snooze_days: int

    ui_host: str
    ui_port: int

    analyse_minutes: int
    analyse_min_watch_hours: float
    analyse_budget_usd: float
    analyse_timeout_minutes: int
    claude_command: str

    scan_minutes: int  # while the source scanner is on (Go on the page), a scan this often
    scan_budget_usd: float  # most one scan may spend
    scan_endpoints: int  # endpoints reviewed per scan

    fix_worktree: str  # Fix this: a git worktree of OBP-API that Claude fixes findings in, one commit each
    fix_budget_usd: float  # most one fix may spend

    @property
    def is_app(self) -> bool:
        """An app with its own /monitoring endpoints rather than OBP-API: no metrics, root, Scopes or source review."""
        return bool(self.monitoring_path)

    @property
    def env_prefix(self) -> str:
        """What this instance's settings are prefixed with in the environment ("" for the default instance)."""
        return _prefix(self.name)

    @classmethod
    def all_from_env(cls) -> list["Config"]:
        """Every instance: each OBP-API instance, its app directory read again, followed by its apps."""
        for name in _obp_instance_names():
            discover_apps(cls.from_env(name))
        return [cls.from_env(name) for name in instance_names()]

    @classmethod
    def from_env(cls, name: str = DEFAULT_INSTANCE) -> "Config":
        obp_names = _obp_instance_names()
        parent, app = _app_instances(obp_names).get(name, ("", "")) if name not in obp_names else ("", "")
        is_app = bool(app)
        named = name != DEFAULT_INSTANCE
        # An app's own settings, then its OBP-API instance's, then the plain ones
        prefixes = [_prefix(name), _prefix(parent)] if is_app else [_prefix(name)]

        def env(key: str, default: str = "") -> str:
            if not named:
                return os.environ.get(key, default)
            for i, prefix in enumerate(prefixes + [""]):
                if i > 0 and (key in OWN_PER_INSTANCE or (is_app and key in OBP_API_ONLY)):
                    break
                if (value := os.environ.get(prefix + key)) is not None:
                    return value
            return default

        monitoring_path = APPS[app].monitoring_path if is_app else ""
        return cls(
            name=name,
            obp_base_url=(_app_urls(parent)[app] if is_app
                          else env("OBP_BASE_URL", "http://localhost:8080")).rstrip("/"),
            monitoring_path=monitoring_path,
            parent=parent,
            oidc_issuer=env("OIDC_ISSUER", "http://localhost:9000/obp-oidc").rstrip("/"),
            oidc_client_id=env("OIDC_CLIENT_ID", ""),
            oidc_client_secret=env("OIDC_CLIENT_SECRET", ""),
            log_cache_api_version=env("OBP_LOG_CACHE_API_VERSION", "v5.1.0"),
            telemetry_api_version=env("OBP_TELEMETRY_API_VERSION", "v7.0.0"),
            aggregate_metrics_api_version=env("OBP_AGGREGATE_METRICS_API_VERSION", "v6.0.0"),
            root_api_version=env("OBP_ROOT_API_VERSION", "v5.1.0"),
            obp_api_source=os.environ.get(APPS[app].source_setting, "") if is_app else env("OBP_API_SOURCE", ""),
            obp_api_github_repo=APPS[app].github_repo if is_app else env("OBP_API_GITHUB_REPO", "OpenBankProject/OBP-API"),
            db_path=env("SENTINEL_DB", f"sentinel-{name}.db" if named else "sentinel.db"),
            source_db_path=env("SENTINEL_SOURCE_DB", "sentinel-source.db"),
            review_dir=env("SENTINEL_REVIEW_DIR", "source-review"),
            digest_dir=env("SENTINEL_DIGEST_DIR", f"digests/{name}" if named else "digests"),
            poll_seconds=int(env("SENTINEL_POLL_SECONDS", "120")),
            log_levels=_list(env("SENTINEL_LOG_LEVELS", "error,warning")),
            fetch_limit=int(env("SENTINEL_FETCH_LIMIT", "1000")),
            bucket_minutes=int(env("SENTINEL_BUCKET_MINUTES", "15")),
            # An app's Telemetry is small: keep all of it unless told otherwise
            telemetry_prefixes=_list(
                env("SENTINEL_TELEMETRY_PREFIXES",
                    "" if monitoring_path else "obp.api.,hikaricp.connections,jvm.memory.used,jvm.gc.pause")
            ),
            ignore_regex=env("SENTINEL_IGNORE_REGEX",
                             "monitoring/" if monitoring_path else
                             "system/log-cache|management/telemetry|management/aggregate-metrics|consumers/current/platform-app"),
            retention_days=int(env("SENTINEL_RETENTION_DAYS", "14")),
            min_watch_hours=float(env("SENTINEL_MIN_WATCH_HOURS", "4")),
            digest_size=int(env("SENTINEL_DIGEST_SIZE", "3")),
            max_per_week=int(env("SENTINEL_MAX_PER_WEEK", "10")),
            min_priority=float(env("SENTINEL_MIN_PRIORITY", "1.0")),
            snooze_days=int(env("SENTINEL_SNOOZE_DAYS", "7")),
            ui_host=env("SENTINEL_UI_HOST", "127.0.0.1"),
            ui_port=int(env("SENTINEL_UI_PORT", "8765")),
            analyse_minutes=int(env("SENTINEL_ANALYSE_MINUTES", "60")),
            analyse_min_watch_hours=float(env("SENTINEL_ANALYSE_MIN_WATCH_HOURS", "2")),
            analyse_budget_usd=float(env("SENTINEL_ANALYSE_BUDGET_USD", "2")),
            analyse_timeout_minutes=int(env("SENTINEL_ANALYSE_TIMEOUT_MINUTES", "30")),
            claude_command=env("SENTINEL_CLAUDE_COMMAND", "claude"),
            scan_minutes=int(env("SENTINEL_SCAN_MINUTES", "10")),
            scan_budget_usd=float(env("SENTINEL_SCAN_BUDGET_USD", "0.5")),
            scan_endpoints=int(env("SENTINEL_SCAN_ENDPOINTS", "2")),
            fix_worktree=env("SENTINEL_FIX_WORKTREE", ""),
            fix_budget_usd=float(env("SENTINEL_FIX_BUDGET_USD", "3")),
        )
