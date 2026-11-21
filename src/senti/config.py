"""Settings, read from the environment (and a .env file in the working directory).

Senti can watch several OBP-API instances: list their names in SENTINEL_INSTANCES (e.g. "local,staging").
Any setting can then be given per instance by prefixing it with the instance's name in capitals, `-` as `_`
(e.g. STAGING_OBP_BASE_URL, STAGING_OBP_API_SOURCE); without a prefixed value the plain one is used. Each
instance has its own database (sentinel-<name>.db) and digests (digests/<name>): SENTINEL_DB and
SENTINEL_DIGEST_DIR are only taken prefixed, so instances never share them. Without SENTINEL_INSTANCES
there is one instance, "default", using sentinel.db and digests/ as before.

Each OBP-API instance can have apps that serve their own log cache and Telemetry in OBP-API's shape: API
Explorer II, the API Manager, the Portal, Cohort, OBP-OIDC and the Sandbox Populator. They are found in the instance's public app directory
(GET /obp/v6.0.0/app-directory), read at startup and kept in the instance's database for when OBP-API cannot be
reached. Each becomes an instance of its own, named <instance>-<app> (local-explorer), with its own database and
digests. It uses its OBP-API instance's settings, OIDC client included, except SENTINEL_TELEMETRY_PREFIXES and
SENTINEL_IGNORE_REGEX, which are written for OBP-API: an app takes those only prefixed with its own name
(LOCAL_EXPLORER_SENTINEL_IGNORE_REGEX). Its source is EXPLORER_SOURCE (API Explorer II), FRONTEND_SOURCE
(OBP-Frontend, for the API Manager, the Portal, Cohort and the Sandbox Populator) or OIDC_SOURCE (OBP-OIDC),
shared by all instances.

With a senti.toml (see model.py), the OBP-API instances, their URLs, OIDC clients and checkouts come from that file
instead, and the apps' checkouts from its services. The tuning settings (SENTINEL_*) still come from the environment.
Every instance has a service, a kind and an environment: without senti.toml, the environment is the name of the
instance (or of an app's OBP-API instance).
"""

import logging
import os
import re
from dataclasses import dataclass

from dotenv import find_dotenv, load_dotenv

from .packs.obp import APP_NOTES
from .packs.obp.app_directory import APPS, kept_directory, read_app_directory
from .model import OBP_API, Environment, Instance, Service, Setup, SetupError, load as load_setup, setup_path


def _list(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


DEFAULT_INSTANCE = "default"
INSTANCE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")
OWN_PER_INSTANCE = {"SENTINEL_DB", "SENTINEL_DIGEST_DIR"}
# Written for OBP-API's meters and log lines: an app takes them only prefixed with its own name
OBP_API_ONLY = {"SENTINEL_TELEMETRY_PREFIXES", "SENTINEL_IGNORE_REGEX"}


OBP_API_SERVICE = "obp-api"
OBP_API_REPO = "OpenBankProject/OBP-API"
logger = logging.getLogger(__name__)
_directories: dict[str, dict[str, str]] = {}  # by OBP-API instance: its apps' URLs, by app


def _prefix(name: str) -> str:
    return "" if name == DEFAULT_INSTANCE else name.upper().replace("-", "_") + "_"


class ConfigError(Exception):
    pass


_setups: dict[tuple, Setup | None] = {}


def current_setup() -> Setup | None:
    """The setup in senti.toml, read again when the file changes; None without the file."""
    load_dotenv(find_dotenv(usecwd=True))  # the working directory's .env, not one near the code
    path = setup_path()
    try:
        key = (str(path.resolve()), path.stat().st_mtime_ns)
    except OSError:
        return None
    if key not in _setups:
        try:
            _setups[key] = load_setup(path)
        except SetupError as e:
            raise ConfigError(str(e)) from e
    return _setups[key]


def _obp_instance_names() -> list[str]:
    load_dotenv(find_dotenv(usecwd=True))  # the working directory's .env, not one near the code
    if setup := current_setup():
        if not setup.instances:
            raise ConfigError(f"{setup.path} lists no instances")
        return [i.id for i in setup.instances]
    names = _list(os.environ.get("SENTINEL_INSTANCES", ""))
    for name in names:
        if not INSTANCE_NAME.match(name):
            raise ConfigError(f"Instance name {name!r} in SENTINEL_INSTANCES: use lower case letters, digits and -")
    if len(set(names)) != len(names):
        raise ConfigError("SENTINEL_INSTANCES names an instance twice")
    return names or [DEFAULT_INSTANCE]


def discover_apps(config: "Config") -> dict[str, str]:
    """The URLs of an OBP-API instance's apps, by app, from its app directory (or as kept when it cannot be read)."""
    urls = read_app_directory(config)
    _directories[config.name] = urls
    return urls


def app_url(config: "Config", app: str) -> str | None:
    """An app of the OBP-API instance this configuration belongs to, as last found in its app directory."""
    return _app_urls(config.parent or config.name).get(app)


def api_manager_url(config: "Config") -> str | None:
    """The API Manager of the OBP-API instance this configuration belongs to, as last found in its app directory."""
    return app_url(config, "manager")


def _app_urls(parent: str) -> dict[str, str]:
    """An OBP-API instance's apps as last discovered (in this process, else as kept in its database)."""
    if parent not in _directories:
        _directories[parent] = kept_directory(Config.from_env(parent))
    return _directories[parent]


def _app_instances(obp_names: list[str]) -> dict[str, tuple[str, str]]:
    """The apps in each OBP-API instance's app directory, by instance name: (their OBP-API instance, which app)."""
    apps = {}
    setup = current_setup()
    for parent in obp_names:
        if setup and not setup.instance(parent).discover:
            continue
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
    name: str  # the OBP-API instance (or app) this configuration watches: its instance id
    service: str  # the service it is an instance of (service.name)
    kind: str  # what the service's code is (model.KINDS); what Senti learns is kept per kind
    environment: str  # local, test, staging, production... (deployment.environment.name)
    service_notes: str  # what every AI run is told about this service, after its kind's notes (context.py)
    obp_base_url: str  # for an app, the app's own URL
    monitoring_path: str  # "" for OBP-API; for an app, where its log-cache and telemetry endpoints are
    parent: str  # for an app, its OBP-API instance; "" for OBP-API
    oidc_issuer: str
    oidc_client_id: str
    oidc_client_secret: str
    oidc_client_setting: str  # the setting the OIDC client id was read from (an app's is usually its OBP-API instance's)
    obp_api_url: str  # the OBP-API that checks Senti's token: obp_base_url, or for an app its instance's
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

    fix_worktree: str  # Fix this: Senti's own git worktree of OBP-API, next to OBP_API_SOURCE, made when first needed
    fix_budget_usd: float  # most one fix may spend

    build_actions_url: str  # the OBP-API build on GitHub Actions, shown at the top of the page (build.py)
    sonarcloud_project: str  # SonarCloud's key for OBP-API, read as another source of findings (sonar.py); "" for none

    @property
    def is_app(self) -> bool:
        """An app with its own /monitoring endpoints rather than OBP-API: no metrics, root, Scopes or source review."""
        return bool(self.monitoring_path)

    @property
    def source(self) -> str:
        """The data source it is read from (sources/): for now an OBP app's or OBP-API's, until senti.toml names it."""
        return "obp-app" if self.is_app else "obp"

    @property
    def env_prefix(self) -> str:
        """What this instance's settings are prefixed with in the environment ("" for the default instance)."""
        return _prefix(self.name)

    @classmethod
    def all_from_env(cls) -> list["Config"]:
        """Every instance: each OBP-API instance, its app directory read again, followed by its apps."""
        setup = current_setup()
        for name in _obp_instance_names():
            if not setup or setup.instance(name).discover:
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

        def setting(key: str) -> str | None:
            """The setting `key` is read from: prefixed with this instance's name, its OBP-API instance's, or plain."""
            if not named:
                return key if key in os.environ else None
            for i, prefix in enumerate(prefixes + [""]):
                if i > 0 and (key in OWN_PER_INSTANCE or (is_app and key in OBP_API_ONLY)):
                    break
                if prefix + key in os.environ:
                    return prefix + key
            return None

        def env(key: str, default: str = "") -> str:
            name = setting(key)
            return os.environ[name] if name else default

        monitoring_path = APPS[app].monitoring_path if is_app else ""
        # With senti.toml: the OBP-API instance (an app's parent), its service, and the app's service if listed
        setup = current_setup()
        listed = setup.instance(parent or name) if setup else None
        obp_service = setup.service(listed.service) if listed else None
        app_service = setup.service(APPS[app].service) if setup and is_app else None
        credentials = listed.credentials if listed else ""

        def oidc(key: str, default: str = "") -> str:
            """An OIDC client setting: <credentials>_<key> with senti.toml, else OIDC_<key> as before."""
            return os.environ.get(f"{credentials}_{key}", default) if listed else env(f"OIDC_{key}", default)

        if listed:
            obp_api_source = obp_service.source
            source = app_service.source if app_service else "" if is_app else obp_api_source
            github_repo = ((app_service and app_service.github_repo) or APPS[app].github_repo if is_app
                           else obp_service.github_repo)
        else:
            obp_api_source = env("OBP_API_SOURCE", "")
            source = os.environ.get(APPS[app].source_setting, "") if is_app else obp_api_source
            github_repo = APPS[app].github_repo if is_app else env("OBP_API_GITHUB_REPO", OBP_API_REPO)
        if is_app:
            service, kind = (app_service.name, app_service.kind) if app_service else (APPS[app].service, APPS[app].kind)
        else:
            service, kind = (obp_service.name, obp_service.kind) if listed else (OBP_API_SERVICE, OBP_API)
        return cls(
            name=name,
            service=service,
            kind=kind,
            environment=listed.environment if listed else (parent or name),
            service_notes=((app_service and app_service.notes)
                           or APP_NOTES + (f" Its code is in `{APPS[app].folder}/` of its checkout." if APPS[app].folder else "")
                           if is_app else obp_service.notes if listed else ""),
            obp_base_url=(_app_urls(parent)[app] if is_app else listed.url if listed
                          else env("OBP_BASE_URL", "http://localhost:8080")).rstrip("/"),
            monitoring_path=monitoring_path,
            parent=parent,
            oidc_issuer=oidc("ISSUER", "http://localhost:9000/obp-oidc").rstrip("/"),
            oidc_client_id=oidc("CLIENT_ID"),
            oidc_client_secret=oidc("CLIENT_SECRET"),
            oidc_client_setting=(f"{credentials}_CLIENT_ID" if listed
                                 else setting("OIDC_CLIENT_ID") or _prefix(name) + "OIDC_CLIENT_ID"),
            obp_api_url=(Config.from_env(parent).obp_base_url if is_app else listed.url if listed
                         else env("OBP_BASE_URL", "http://localhost:8080").rstrip("/")),
            log_cache_api_version=env("OBP_LOG_CACHE_API_VERSION", "v5.1.0"),
            telemetry_api_version=env("OBP_TELEMETRY_API_VERSION", "v7.0.0"),
            aggregate_metrics_api_version=env("OBP_AGGREGATE_METRICS_API_VERSION", "v6.0.0"),
            root_api_version=env("OBP_ROOT_API_VERSION", "v5.1.0"),
            obp_api_source=source,
            obp_api_github_repo=github_repo,
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
            analyse_budget_usd=float(env("SENTINEL_ANALYSE_BUDGET_USD", "10")),
            analyse_timeout_minutes=int(env("SENTINEL_ANALYSE_TIMEOUT_MINUTES", "30")),
            claude_command=env("SENTINEL_CLAUDE_COMMAND", "claude"),
            scan_minutes=int(env("SENTINEL_SCAN_MINUTES", "10")),
            scan_budget_usd=float(env("SENTINEL_SCAN_BUDGET_USD", "3")),
            scan_endpoints=int(env("SENTINEL_SCAN_ENDPOINTS", "2")),
            fix_worktree=(obp_api_source.rstrip("/") + "-sentinel-fixes") if obp_api_source else "",
            fix_budget_usd=float(env("SENTINEL_FIX_BUDGET_USD", "15")),
            build_actions_url="" if is_app else obp_service.build if listed else env("OBP_API_BUILD_ACTIONS_URL", ""),
            sonarcloud_project="" if is_app else obp_service.sonarcloud if listed else env("SONARCLOUD_PROJECT", ""),
        )


def setup_from_env() -> tuple[Setup, list[str]]:
    """Today's setup from the environment, as senti.toml says it, for `senti config import-env`; and what to add
    to .env before using it. Each instance keeps its name as its id, so its database and digests stay where they are,
    and its name becomes its environment's (rename it in the file: the id stays)."""
    if current_setup():
        raise ConfigError(f"{setup_path()} exists already: Senti reads its setup from there")
    services: dict[tuple, Service] = {}
    instances, environments, missing = [], [], []
    for name in _obp_instance_names():
        c = Config.from_env(name)
        found = (c.obp_api_source, c.obp_api_github_repo, c.build_actions_url, c.sonarcloud_project)
        if found not in services:
            service_name = OBP_API_SERVICE if not services else f"{OBP_API_SERVICE}-{name}"
            services[found] = Service(service_name, OBP_API,
                                      f"https://github.com/{c.obp_api_github_repo}" if c.obp_api_github_repo else "",
                                      *found[:1], *found[2:])
        credentials = c.oidc_client_setting.removesuffix("_CLIENT_ID")
        for key in ("CLIENT_ID", "CLIENT_SECRET", "ISSUER"):
            if f"{credentials}_{key}" not in os.environ:
                missing.append(f"{credentials}_{key}")
        environments.append(Environment(name))
        instances.append(Instance(name, services[found].name, name, c.obp_base_url, credentials))
    apps = []
    for a in {app.service: app for app in APPS.values()}.values():
        if source := os.environ.get(a.source_setting, ""):
            apps.append(Service(a.service, a.kind, f"https://github.com/{a.github_repo}", source))
    return Setup("obp", (*services.values(), *apps), tuple(environments), tuple(instances)), missing
