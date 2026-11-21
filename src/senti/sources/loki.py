"""The `loki` source: an instance's log lines, from Loki's HTTP query API (GET <url>/loki/api/v1/query_range). The
instance's lines are those of its stream selector in senti.toml, such as {app="obp-api",env="local"}.

A level's lines are those whose text names it (ERROR, WARN or WARNING, ...), case aside: most log formats write the
level as a word, and Loki 2 has no level of its own. The newest come first, as the collector expects; lines are read
from the last hour, at most the fetch limit, so a busy service is read like OBP-API's log cache.
"""

import os
import time
from typing import TYPE_CHECKING

import httpx

from . import LOGS, Source

if TYPE_CHECKING:
    from ..config import Config

LEVEL_WORDS = {"error": "error|fatal|severe|critical", "warning": "warn|warning", "info": "info",
               "debug": "debug", "trace": "trace"}
WINDOW_SECONDS = 3600


class LokiError(Exception):
    pass


class LokiSource(Source):
    name = "loki"
    offers = frozenset({LOGS})

    def __init__(self, config: "Config", http: httpx.Client | None = None):
        super().__init__(config)
        self.http = http or httpx.Client(timeout=30)

    def query(self, level: str, limit: int) -> str:
        words = LEVEL_WORDS.get(level, level)
        return f'{self.config.loki_selector} |~ "(?i)\\\\b({words})\\\\b"'

    def logs(self, level: str, limit: int) -> list[str]:
        if not self.config.loki_selector:
            raise LokiError(f"{self.config.name} has no loki selector in senti.toml")
        url = f"{self.config.loki_url}/loki/api/v1/query_range"
        end = time.time_ns()
        params = {"query": self.query(level, limit), "limit": str(limit), "direction": "backward",
                  "start": str(end - WINDOW_SECONDS * 1_000_000_000), "end": str(end)}
        headers = {"Authorization": f"Bearer {token}"} if (token := os.environ.get("LOKI_TOKEN")) else {}
        try:
            response = self.http.get(url, params=params, headers=headers)
        except httpx.HTTPError as e:
            raise LokiError(f"GET {url} failed: {e}") from e
        if response.status_code != 200:
            raise LokiError(f"GET {url} failed ({response.status_code}): {response.text[:300]}")
        streams = response.json().get("data", {}).get("result", [])
        lines = [(int(ts), line) for stream in streams for ts, line in stream.get("values", [])]
        return [line for _, line in sorted(lines, key=lambda x: x[0], reverse=True)[:limit]]
