"""The collector: plain code, no LLM. Polls OBP-API and turns what it sees into counted signatures.

The log cache returns each level's most recent entries, newest first, without ids. To find which
entries arrived since the last poll, we remember the newest few messages we saw (the "head") and
look for them in the next response: everything above them is new.
"""

import json
import logging
import re
import time

from .config import Config
from .obp_client import OBPClient
from .signatures import parse_line, signature_of
from .store import Store, now

logger = logging.getLogger(__name__)

HEAD_SIZE = 5
PRUNE_EVERY_SECONDS = 3600


def new_entries(current: list[str], previous_head: list[str]) -> tuple[list[str], bool]:
    """Entries of `current` (newest first) that are newer than `previous_head`, and whether some may be missing.

    On the first poll there is no head and the whole response counts as new (the backlog).
    If the head is not found, OBP-API trimmed past it: every entry is new and some were missed.
    """
    if not previous_head:
        return current, False
    for i in range(len(current)):
        window = current[i : i + len(previous_head)]
        # Near the end of the response only part of the head may be left: accept that if at least two match.
        if window == previous_head[: len(window)] and (len(window) == len(previous_head) or len(window) >= 2):
            return current[:i], False
    return current, True


def bucket_of(ts: int, bucket_minutes: int) -> int:
    size = bucket_minutes * 60
    return ts - ts % size


class Collector:
    def __init__(self, config: Config, store: Store, client: OBPClient):
        self.config = config
        self.store = store
        self.client = client
        self.ignore = re.compile(config.ignore_regex) if config.ignore_regex else None

    def ingest(self, level: str, messages: list[str]) -> int:
        """Count new messages (oldest first, so samples and first_seen are in order). Returns how many counted."""
        counted = 0
        poll_ts = now()
        for raw in reversed(messages):
            if self.ignore and self.ignore.search(raw):
                continue
            parsed = parse_line(raw)
            ts = int(parsed.ts.timestamp()) if parsed.ts else poll_ts
            self.store.record_occurrence(
                signature_of(level, parsed), ts, bucket_of(ts, self.config.bucket_minutes), raw
            )
            counted += 1
        return counted

    def poll_logs(self, level: str) -> None:
        source = f"log:{level}"
        try:
            current = self.client.log_cache(level, self.config.fetch_limit)
        except Exception as e:  # keep watching: a failed poll is recorded and shows up as reduced coverage
            logger.warning("Polling %s failed: %s", source, e)
            self.store.record_poll(source, ok=False, error=str(e)[:500])
            return
        head_key = f"head:{level}"
        fresh, gap = new_entries(current, self.store.get_state(head_key, []))
        if gap:
            logger.warning("%s: last poll's entries were no longer in the cache; some entries were missed", source)
        counted = self.ingest(level, fresh)
        if current:
            self.store.set_state(head_key, current[:HEAD_SIZE])
        self.store.record_poll(source, ok=True, fetched=len(current), new=counted, gap=gap)

    def poll_telemetry(self) -> None:
        try:
            body = self.client.telemetry()
        except Exception as e:
            logger.warning("Polling telemetry failed: %s", e)
            self.store.record_poll("telemetry", ok=False, error=str(e)[:500])
            return
        prefixes = tuple(self.config.telemetry_prefixes)
        values = [
            (meter["name"], meter["type"], _tags_json(meter.get("tags", {})), stat, value)
            for meter in body.get("meters", [])
            if meter["name"].startswith(prefixes)
            for stat, value in meter.get("measurements", {}).items()
        ]
        self.store.record_telemetry(now(), body.get("api_instance_id"), body.get("git_commit"), values)
        self.store.record_poll("telemetry", ok=True, fetched=len(values), new=len(values))

    def poll_once(self) -> None:
        for level in self.config.log_levels:
            self.poll_logs(level)
        self.poll_telemetry()
        last_prune = self.store.get_state("last_prune", 0)
        if now() - last_prune > PRUNE_EVERY_SECONDS:
            self.store.prune(self.config.retention_days)
            self.store.set_state("last_prune", now())
        self.store.commit()

    def run_forever(self) -> None:
        logger.info(
            "Watching %s every %ss (levels: %s)",
            self.config.obp_base_url, self.config.poll_seconds, ",".join(self.config.log_levels),
        )
        while True:
            started = time.monotonic()
            self.poll_once()
            time.sleep(max(0.0, self.config.poll_seconds - (time.monotonic() - started)))


def _tags_json(tags: dict) -> str:
    return json.dumps(tags, sort_keys=True)
