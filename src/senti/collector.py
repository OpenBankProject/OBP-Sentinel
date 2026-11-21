"""The collector: plain code, no LLM. Polls an instance's data source and turns what it sees into counted signatures,
Telemetry values, metric buckets and deployments. What to read and how comes from the source (sources/); how to read
a log line comes from the service's kind (kinds/). The collector itself knows nothing about OBP.

A source returns each level's most recent log lines, newest first, without ids. To find which
entries arrived since the last poll, we remember the newest few messages we saw (the "head") and
look for them in the next response: everything above them is new.
"""

import json
import logging
import re
import time

from .config import Config
from .kinds import kind_of
from .signatures import parse_line, signature_of
from .source import reach_is_due
from .sources import COMMIT, LOGS, METRICS, REACH, TELEMETRY, Source
from .store import Store, now

logger = logging.getLogger(__name__)

HEAD_SIZE = 5
PRUNE_EVERY_SECONDS = 3600
# A source may write metrics asynchronously: only ask for a bucket this long after it has ended
METRICS_LAG_SECONDS = 120
# After a pause, catch up at most this many buckets per poll
METRICS_MAX_BUCKETS_PER_POLL = 8
# How often to check that the endpoints Senti reads refuse a call with no token
SECURED_CHECK_SECONDS = 3600
SECURED = "endpoints_secured"


def new_entries(current: list[str], previous_head: list[str]) -> tuple[list[str], bool]:
    """Entries of `current` (newest first) that are newer than `previous_head`, and whether some may be missing.

    On the first poll there is no head and the whole response counts as new (the backlog).
    If the head is not found, the source trimmed past it: every entry is new and some were missed.
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
    def __init__(self, config: Config, store: Store, source: Source):
        self.config = config
        self.store = store
        self.source = source
        self.kind = kind_of(config.kind)
        self.ignore = re.compile(config.ignore_regex) if config.ignore_regex else None

    def ingest(self, level: str, messages: list[str]) -> int:
        """Count new messages (oldest first, so samples and first_seen are in order). Returns how many counted."""
        counted = 0
        poll_ts = now()
        for raw in reversed(messages):
            if self.ignore and self.ignore.search(raw):
                continue
            parsed = parse_line(raw, self.kind)
            ts = int(parsed.ts.timestamp()) if parsed.ts else poll_ts
            self.store.record_occurrence(
                signature_of(level, parsed, self.kind), ts, bucket_of(ts, self.config.bucket_minutes), raw
            )
            counted += 1
        return counted

    def poll_logs(self, level: str) -> str:
        """Returns a few words for the poll's log line."""
        source = f"log:{level}"
        try:
            current = self.source.logs(level, self.config.fetch_limit)
        except Exception as e:  # keep watching: a failed poll is recorded and shows up as reduced coverage
            logger.warning("Polling %s failed: %s", source, e)
            self.store.record_poll(source, ok=False, error=str(e)[:500])
            return f"{level} failed"
        head_key = f"head:{level}"
        fresh, gap = new_entries(current, self.store.get_state(head_key, []))
        if gap:
            logger.warning("%s: last poll's entries were no longer in the cache; some entries were missed", source)
        counted = self.ingest(level, fresh)
        if current:
            self.store.set_state(head_key, current[:HEAD_SIZE])
        self.store.record_poll(source, ok=True, fetched=len(current), new=counted, gap=gap)
        return f"{level} +{counted}" + (" (some missed)" if gap else "")

    def poll_telemetry(self) -> str:
        try:
            body = self.source.telemetry()
        except Exception as e:
            logger.warning("Polling telemetry failed: %s", e)
            self.store.record_poll("telemetry", ok=False, error=str(e)[:500])
            return "telemetry failed"
        prefixes = tuple(self.config.telemetry_prefixes)
        values = [
            (meter["name"], meter["type"], _tags_json(meter.get("tags", {})), stat, value)
            for meter in body.get("meters", [])
            if not prefixes or meter["name"].startswith(prefixes)
            for stat, value in meter.get("measurements", {}).items()
        ]
        self.store.record_telemetry(now(), body.get("api_instance_id"), body.get("git_commit"), values)
        if COMMIT not in self.source.offers:  # the Telemetry says which commit runs, if the source cannot say alone
            self.store.record_deployment(body.get("git_commit"))
        self.store.record_poll("telemetry", ok=True, fetched=len(values), new=len(values))
        return f"telemetry {len(values)} values"

    def poll_metrics(self) -> str:
        """Fetch the metrics for each bucket that has ended since the last one fetched."""
        size = self.config.bucket_minutes * 60
        last_complete = bucket_of(now() - METRICS_LAG_SECONDS, self.config.bucket_minutes) - size
        oldest_kept = bucket_of(now() - self.config.retention_days * 86400, self.config.bucket_minutes)
        # First run: start with the last complete bucket rather than backfilling history
        start = max(self.store.get_state("metrics_next_bucket", last_complete), oldest_kept)
        fetched = 0
        while start <= last_complete and fetched < METRICS_MAX_BUCKETS_PER_POLL:
            try:
                metrics = self.source.metrics(start, start + size)
            except Exception as e:
                logger.warning("Polling metrics failed: %s", e)
                self.store.record_poll("metrics", ok=False, error=str(e)[:500])
                return "metrics failed"
            self.store.record_metric_bucket(start, metrics)
            start += size
            fetched += 1
            self.store.set_state("metrics_next_bucket", start)
        self.store.record_poll("metrics", ok=True, fetched=fetched, new=fetched)
        return f"metrics {fetched} bucket{'s' if fetched != 1 else ''}"

    def poll_commit(self) -> str:
        """Which commit the instance runs. Not a poll that counts as watching: it needs no token. Kept as the
        "root" state, after OBP-API's root endpoint, which health.py reports on."""
        try:
            git_commit = self.source.running_commit()
        except Exception as e:
            logger.warning("Reading the running commit failed: %s", e)
            self.store.set_state("root", {"ts": now(), "ok": False, "error": str(e)[:500]})
            return "root failed"
        self.store.set_state("root", {"ts": now(), "ok": True})
        self.store.record_deployment(git_commit)
        return f"commit {(git_commit or '?')[:12]}"

    def poll_reach(self) -> str:
        """Which endpoints the instance serves and who can reach them, for the source review. Daily."""
        deployment = self.store.db.execute("SELECT git_commit FROM deployments ORDER BY id DESC LIMIT 1").fetchone()
        git_commit = deployment["git_commit"] if deployment else None
        if not reach_is_due(self.store, git_commit) or now() - self.store.get_state("reach_failed_at", 0) < 3600:
            return "reach unchanged"
        try:
            reach = self.source.reach(git_commit)
        except Exception as e:  # the endpoint lists are large: wait an hour before trying again
            logger.warning("Reading the endpoints the instance serves failed: %s", e)
            self.store.set_state("reach_failed_at", now())
            return "reach failed"
        self.store.set_state("reach", reach)
        return f"reach {len(reach['endpoints'])} endpoints" + ("" if reach["roles"] is not None else ", Roles held unknown")

    def check_secured(self) -> str:
        """Whether the endpoints Senti reads refuse a call with no token, as they must. Hourly, or every poll
        while it cannot tell. Secured if each refuses (401, 403); open if any answers 200; otherwise it cannot tell
        (no answer, 404 ...)."""
        last = self.store.get_state(SECURED, {})
        if last.get("verdict") != "unknown" and now() - last.get("ts", 0) < SECURED_CHECK_SECONDS:
            return "secured unchanged"
        checks = [{"path": path, "status": self.source.status_without_token(self.config.obp_base_url + path)}
                  for path in self.source.secured_paths()]
        if not checks:
            return "nothing to check without a token"
        statuses = [c["status"] for c in checks]
        verdict = "open" if 200 in statuses else "secured" if all(s in (401, 403) for s in statuses) else "unknown"
        self.store.set_state(SECURED, {"ts": now(), "verdict": verdict, "checks": checks})
        if verdict == "open":
            logger.warning("Answers without a token: %s",
                           ", ".join(self.config.obp_base_url + c["path"] for c in checks if c["status"] == 200))
        return f"endpoints {verdict} without a token"

    def poll_once(self) -> None:
        """One poll of whatever the source offers, in this order."""
        offers = self.source.offers
        self.source.before_poll(self.store)
        results = [self.poll_commit()] if COMMIT in offers else []
        if LOGS in offers:
            results += [self.poll_logs(level) for level in self.config.log_levels]
        results += [self.poll_telemetry()] if TELEMETRY in offers else []
        results += [self.poll_metrics()] if METRICS in offers else []
        results += [self.poll_reach()] if REACH in offers else []
        results.append(self.check_secured())
        if after := self.source.after_poll(self.store):
            results.append(after)
        logger.info("Polled: %s", ", ".join(results))
        last_prune = self.store.get_state("last_prune", 0)
        if now() - last_prune > PRUNE_EVERY_SECONDS:
            self.store.prune(self.config.retention_days)
            self.store.set_state("last_prune", now())
        self.store.commit()

    def run_forever(self) -> None:
        logger.info(
            "Watching %s through the %s source every %ss (levels: %s)",
            self.config.obp_base_url + self.config.monitoring_path, self.source.name, self.config.poll_seconds,
            ",".join(self.config.log_levels),
        )
        while True:
            started = time.monotonic()
            self.poll_once()
            time.sleep(max(0.0, self.config.poll_seconds - (time.monotonic() - started)))


def _tags_json(tags: dict) -> str:
    return json.dumps(tags, sort_keys=True)
