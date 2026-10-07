"""Settings, read from the environment (and a .env file in the working directory).

Sentinel can watch several OBP-API instances: list their names in SENTINEL_INSTANCES (e.g. "local,staging").
Any setting can then be given per instance by prefixing it with the instance's name in capitals, `-` as `_`
(e.g. STAGING_OBP_BASE_URL, STAGING_OBP_API_SOURCE); without a prefixed value the plain one is used. Each
instance has its own database (sentinel-<name>.db) and digests (digests/<name>): SENTINEL_DB and
SENTINEL_DIGEST_DIR are only taken prefixed, so instances never share them. Without SENTINEL_INSTANCES
there is one instance, "default", using sentinel.db and digests/ as before.
"""

import os
import re
from dataclasses import dataclass

from dotenv import find_dotenv, load_dotenv


def _list(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


DEFAULT_INSTANCE = "default"
INSTANCE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")
OWN_PER_INSTANCE = {"SENTINEL_DB", "SENTINEL_DIGEST_DIR"}


class ConfigError(Exception):
    pass


def instance_names() -> list[str]:
    load_dotenv(find_dotenv(usecwd=True))  # the working directory's .env, not one near the code
    names = _list(os.environ.get("SENTINEL_INSTANCES", ""))
    for name in names:
        if not INSTANCE_NAME.match(name):
            raise ConfigError(f"Instance name {name!r} in SENTINEL_INSTANCES: use lower case letters, digits and -")
    if len(set(names)) != len(names):
        raise ConfigError("SENTINEL_INSTANCES names an instance twice")
    return names or [DEFAULT_INSTANCE]


@dataclass(frozen=True)
class Config:
    name: str  # the OBP-API instance this configuration watches
    obp_base_url: str
    oidc_issuer: str
    oidc_client_id: str
    oidc_client_secret: str
    log_cache_api_version: str
    telemetry_api_version: str
    aggregate_metrics_api_version: str
    root_api_version: str
    obp_api_source: str

    db_path: str
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

    @property
    def env_prefix(self) -> str:
        """What this instance's settings are prefixed with in the environment ("" for the default instance)."""
        return "" if self.name == DEFAULT_INSTANCE else self.name.upper().replace("-", "_") + "_"

    @classmethod
    def all_from_env(cls) -> list["Config"]:
        return [cls.from_env(name) for name in instance_names()]

    @classmethod
    def from_env(cls, name: str = DEFAULT_INSTANCE) -> "Config":
        load_dotenv(find_dotenv(usecwd=True))  # the working directory's .env, not one near the code
        prefix = name.upper().replace("-", "_") + "_"
        named = name != DEFAULT_INSTANCE

        def env(key: str, default: str = "") -> str:
            if not named:
                return os.environ.get(key, default)
            value = os.environ.get(prefix + key)
            if value is None and key not in OWN_PER_INSTANCE:
                value = os.environ.get(key)
            return default if value is None else value

        return cls(
            name=name,
            obp_base_url=env("OBP_BASE_URL", "http://localhost:8080").rstrip("/"),
            oidc_issuer=env("OIDC_ISSUER", "http://localhost:9000/obp-oidc").rstrip("/"),
            oidc_client_id=env("OIDC_CLIENT_ID", ""),
            oidc_client_secret=env("OIDC_CLIENT_SECRET", ""),
            log_cache_api_version=env("OBP_LOG_CACHE_API_VERSION", "v5.1.0"),
            telemetry_api_version=env("OBP_TELEMETRY_API_VERSION", "v7.0.0"),
            aggregate_metrics_api_version=env("OBP_AGGREGATE_METRICS_API_VERSION", "v6.0.0"),
            root_api_version=env("OBP_ROOT_API_VERSION", "v5.1.0"),
            obp_api_source=env("OBP_API_SOURCE", ""),
            db_path=env("SENTINEL_DB", f"sentinel-{name}.db" if named else "sentinel.db"),
            digest_dir=env("SENTINEL_DIGEST_DIR", f"digests/{name}" if named else "digests"),
            poll_seconds=int(env("SENTINEL_POLL_SECONDS", "120")),
            log_levels=_list(env("SENTINEL_LOG_LEVELS", "error,warning")),
            fetch_limit=int(env("SENTINEL_FETCH_LIMIT", "1000")),
            bucket_minutes=int(env("SENTINEL_BUCKET_MINUTES", "15")),
            telemetry_prefixes=_list(
                env("SENTINEL_TELEMETRY_PREFIXES", "obp.api.,hikaricp.connections,jvm.memory.used,jvm.gc.pause")
            ),
            ignore_regex=env("SENTINEL_IGNORE_REGEX", "system/log-cache|management/telemetry|management/aggregate-metrics|consumers/current/platform-app"),
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
        )
