"""Settings, read from the environment (and a .env file in the working directory)."""

import os
from dataclasses import dataclass

from dotenv import load_dotenv


def _list(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


@dataclass(frozen=True)
class Config:
    obp_base_url: str
    oidc_issuer: str
    oidc_client_id: str
    oidc_client_secret: str
    log_cache_api_version: str
    telemetry_api_version: str
    obp_api_source: str

    db_path: str
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

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv()
        env = os.environ.get
        return cls(
            obp_base_url=env("OBP_BASE_URL", "http://localhost:8080").rstrip("/"),
            oidc_issuer=env("OIDC_ISSUER", "http://localhost:9000/obp-oidc").rstrip("/"),
            oidc_client_id=env("OIDC_CLIENT_ID", ""),
            oidc_client_secret=env("OIDC_CLIENT_SECRET", ""),
            log_cache_api_version=env("OBP_LOG_CACHE_API_VERSION", "v5.1.0"),
            telemetry_api_version=env("OBP_TELEMETRY_API_VERSION", "v7.0.0"),
            obp_api_source=env("OBP_API_SOURCE", ""),
            db_path=env("SENTINEL_DB", "sentinel.db"),
            poll_seconds=int(env("SENTINEL_POLL_SECONDS", "120")),
            log_levels=_list(env("SENTINEL_LOG_LEVELS", "error,warning")),
            fetch_limit=int(env("SENTINEL_FETCH_LIMIT", "1000")),
            bucket_minutes=int(env("SENTINEL_BUCKET_MINUTES", "15")),
            telemetry_prefixes=_list(
                env("SENTINEL_TELEMETRY_PREFIXES", "obp.api.,hikaricp.connections,jvm.memory.used,jvm.gc.pause")
            ),
            ignore_regex=env("SENTINEL_IGNORE_REGEX", "system/log-cache|management/telemetry|consumers/current/platform-app"),
            retention_days=int(env("SENTINEL_RETENTION_DAYS", "14")),
            min_watch_hours=float(env("SENTINEL_MIN_WATCH_HOURS", "4")),
            digest_size=int(env("SENTINEL_DIGEST_SIZE", "3")),
            max_per_week=int(env("SENTINEL_MAX_PER_WEEK", "10")),
            min_priority=float(env("SENTINEL_MIN_PRIORITY", "1.0")),
            snooze_days=int(env("SENTINEL_SNOOZE_DAYS", "7")),
        )
