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
from .source import fetch_reach, reach_is_due
from .store import Store, now

logger = logging.getLogger(__name__)

HEAD_SIZE = 5
PRUNE_EVERY_SECONDS = 3600
# Once OBP has accepted the Platform App declaration with every Scope held, declare again this often (an admin may have re-marked it)
REDECLARE_EVERY_SECONDS = 3600
# OBP-API writes metrics asynchronously: only ask for a bucket this long after it has ended
METRICS_LAG_SECONDS = 120
# After a pause, catch up at most this many buckets per poll
METRICS_MAX_BUCKETS_PER_POLL = 8
# How often to check that the endpoints Sentinel reads refuse a call with no token
SECURED_CHECK_SECONDS = 3600
SECURED = "endpoints_secured"
# What OBP-API says about the Roles an app refused Sentinel, and Sentinel's consumer_id (health.py reads it)
APP_ACCESS = "app_access"
# An app's 403: GET <app>/monitoring/... failed (403): {"error":"Requires one of the Roles: A, B"}
APP_REFUSED = re.compile(r"failed \(403\): .*Requires one of the Roles: (?P<roles>[\w, ]+)")


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
        self.declared_at = 0.0

    def declare_platform_app(self) -> None:
        """Tell OBP-API which Scopes Sentinel needs, and say which are missing.

        Refused until an administrator marks Sentinel's Consumer as a Platform App, so this is retried
        on every poll until accepted: marking takes effect without a restart. Polling goes ahead either way.
        """
        if self.declared_at and time.monotonic() - self.declared_at < REDECLARE_EVERY_SECONDS:
            return
        try:
            app = self.client.declare_platform_app()
        except Exception as e:
            logger.warning("Could not declare Sentinel's Scopes (is its Consumer marked as a Platform App?): %s", e)
            state = {"ts": now(), "ok": False, "error": str(e)[:500]}
            # So the operator is told which Consumer to mark by its consumer_id, not by the client id (its key)
            if consumer_id := self._consumer_id():
                state["consumer_id"] = consumer_id
            self.store.set_state("platform_app", state)
            return
        missing = [s["role_name"] for s in app.get("required_scopes", []) if not s.get("held") and not s.get("optional")]
        # While a Scope is missing, ask again on every poll, so a grant shows within a poll, as the health page says
        self.declared_at = 0.0 if missing else time.monotonic()
        self.store.set_state("platform_app", {"ts": now(), "ok": True, "consumer_id": app.get("consumer_id"),
                                              "label": app.get("label"), "missing": missing})
        if missing:
            logger.warning("Consumer %s lacks the Scopes %s; ask an administrator to grant them",
                           app.get("consumer_id"), ", ".join(missing))
        else:
            logger.info("Declared as Platform App %r; all Scopes held", app.get("label"))

    def _consumer_id(self) -> str | None:
        """Sentinel's consumer_id, asked once; None while OBP-API cannot say (unreachable, token rejected, older version)."""
        if not getattr(self, "consumer_id", None):
            try:
                self.consumer_id = self.client.current_consumer_id()
            except Exception as e:
                logger.info("Could not look up Sentinel's consumer_id: %s", e)
                return None
        return self.consumer_id

    def check_app_access(self) -> str:
        """When the app refuses Sentinel's token (403), ask OBP-API what the app asks it: which Consumer the token is,
        and whether it may use each Role. So the health page can say which Consumer lacks which Scope, or that the
        app refuses Roles OBP-API says Sentinel holds (the app and OBP-API disagree)."""
        errors = [r[0] for r in self.store.db.execute(
            """SELECT p.error FROM polls p JOIN (SELECT source, MAX(id) AS id FROM polls GROUP BY source) l ON l.id = p.id
               WHERE NOT p.ok AND p.error IS NOT NULL""")]
        refused = sorted({role.strip() for e in errors if (m := APP_REFUSED.search(e)) for role in m["roles"].split(",")
                          if role.strip()})
        if not refused:
            self.store.set_state(APP_ACCESS, {"ts": now(), "roles": {}})
            return "access ok"
        roles = {}
        for role in refused:
            try:
                answer = self.client.my_role(role)
                held = answer.get("held_by") or {}
                roles[role] = {"consumer": bool(held.get("consumer")), "user": bool(held.get("user")),
                               "modes": answer.get("allowed_for_auth_modes") or []}
            except Exception as e:  # an OBP-API before v7.0.0 /my/roles, or unreachable: the health page says less
                logger.info("Could not ask OBP-API about %s: %s", role, e)
                roles[role] = {"error": str(e)[:300]}
        self.store.set_state(APP_ACCESS, {"ts": now(), "consumer_id": self._consumer_id(), "roles": roles})
        held = [r for r, a in roles.items() if a.get("consumer") or a.get("user")]
        return f"refused {len(refused)} Roles" + (f", {len(held)} of them held" if held else "")

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

    def poll_logs(self, level: str) -> str:
        """Returns a few words for the poll's log line."""
        source = f"log:{level}"
        try:
            current = self.client.log_cache(level, self.config.fetch_limit)
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
            body = self.client.telemetry()
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
        if self.config.is_app:  # an app has no root endpoint: its Telemetry says which commit it runs
            self.store.record_deployment(body.get("git_commit"))
        self.store.record_poll("telemetry", ok=True, fetched=len(values), new=len(values))
        return f"telemetry {len(values)} values"

    def poll_metrics(self) -> str:
        """Fetch OBP-API's aggregate metrics for each bucket that has ended since the last one fetched."""
        size = self.config.bucket_minutes * 60
        last_complete = bucket_of(now() - METRICS_LAG_SECONDS, self.config.bucket_minutes) - size
        oldest_kept = bucket_of(now() - self.config.retention_days * 86400, self.config.bucket_minutes)
        # First run: start with the last complete bucket rather than backfilling history
        start = max(self.store.get_state("metrics_next_bucket", last_complete), oldest_kept)
        fetched = 0
        while start <= last_complete and fetched < METRICS_MAX_BUCKETS_PER_POLL:
            try:
                metrics = self.client.aggregate_metrics(start, start + size)
            except Exception as e:
                logger.warning("Polling aggregate metrics failed: %s", e)
                self.store.record_poll("metrics", ok=False, error=str(e)[:500])
                return "metrics failed"
            self.store.record_metric_bucket(start, metrics)
            start += size
            fetched += 1
            self.store.set_state("metrics_next_bucket", start)
        self.store.record_poll("metrics", ok=True, fetched=fetched, new=fetched)
        return f"metrics {fetched} bucket{'s' if fetched != 1 else ''}"

    def poll_root(self) -> str:
        """Which commit the instance runs. Not a poll that counts as watching: it needs no token."""
        try:
            root = self.client.root()
        except Exception as e:
            logger.warning("Reading the root endpoint failed: %s", e)
            self.store.set_state("root", {"ts": now(), "ok": False, "error": str(e)[:500]})
            return "root failed"
        self.store.set_state("root", {"ts": now(), "ok": True})
        self.store.record_deployment(root.get("git_commit"))
        return f"commit {(root.get('git_commit') or '?')[:12]}"

    def poll_reach(self) -> str:
        """Which endpoints the instance serves and which Roles anyone holds, for the source review. Daily."""
        deployment = self.store.db.execute("SELECT git_commit FROM deployments ORDER BY id DESC LIMIT 1").fetchone()
        git_commit = deployment["git_commit"] if deployment else None
        if not reach_is_due(self.store, git_commit) or now() - self.store.get_state("reach_failed_at", 0) < 3600:
            return "reach unchanged"
        try:
            reach = fetch_reach(self.client, git_commit)
        except Exception as e:  # the resource docs are large: wait an hour before trying again
            logger.warning("Reading the endpoints the instance serves failed: %s", e)
            self.store.set_state("reach_failed_at", now())
            return "reach failed"
        self.store.set_state("reach", reach)
        return f"reach {len(reach['endpoints'])} endpoints" + ("" if reach["roles"] is not None else ", Roles held unknown")

    def secured_paths(self) -> list[str]:
        """The endpoints Sentinel reads with its token: an app's /monitoring ones, or OBP-API's log cache,
        Telemetry and aggregate metrics."""
        c = self.config
        levels = [*c.log_levels, "all"]
        if c.is_app:
            return [f"{c.monitoring_path}/log-cache/{level}" for level in levels] + [f"{c.monitoring_path}/telemetry"]
        return ([f"/obp/{c.log_cache_api_version}/system/log-cache/{level}" for level in levels]
                + [f"/obp/{c.telemetry_api_version}/management/telemetry",
                   f"/obp/{c.aggregate_metrics_api_version}/management/aggregate-metrics"])

    def check_secured(self) -> str:
        """Whether the endpoints Sentinel reads refuse a call with no token, as they must. Hourly, or every poll
        while it cannot tell. Secured if each refuses (401, 403); open if any answers 200; otherwise it cannot tell
        (no answer, 404 ...)."""
        last = self.store.get_state(SECURED, {})
        if last.get("verdict") != "unknown" and now() - last.get("ts", 0) < SECURED_CHECK_SECONDS:
            return "secured unchanged"
        checks = [{"path": path, "status": self.client.status_without_token(self.config.obp_base_url + path)}
                  for path in self.secured_paths()]
        statuses = [c["status"] for c in checks]
        verdict = "open" if 200 in statuses else "secured" if all(s in (401, 403) for s in statuses) else "unknown"
        self.store.set_state(SECURED, {"ts": now(), "verdict": verdict, "checks": checks})
        if verdict == "open":
            logger.warning("Answers without a token: %s",
                           ", ".join(self.config.obp_base_url + c["path"] for c in checks if c["status"] == 200))
        return f"endpoints {verdict} without a token"

    def poll_once(self) -> None:
        if self.config.is_app:  # only the log cache and Telemetry: Scopes are declared by watching OBP-API itself
            results = [self.poll_logs(level) for level in self.config.log_levels] + [self.poll_telemetry()]
            results += [self.check_secured(), self.check_app_access()]
        else:
            self.declare_platform_app()
            results = [self.poll_root()] + [self.poll_logs(level) for level in self.config.log_levels]
            results += [self.poll_telemetry(), self.poll_metrics(), self.poll_reach(), self.check_secured()]
        logger.info("Polled: %s", ", ".join(results))
        last_prune = self.store.get_state("last_prune", 0)
        if now() - last_prune > PRUNE_EVERY_SECONDS:
            self.store.prune(self.config.retention_days)
            self.store.set_state("last_prune", now())
        self.store.commit()

    def run_forever(self) -> None:
        logger.info(
            "Watching %s every %ss (levels: %s)",
            self.config.obp_base_url + self.config.monitoring_path, self.config.poll_seconds, ",".join(self.config.log_levels),
        )
        while True:
            started = time.monotonic()
            self.poll_once()
            time.sleep(max(0.0, self.config.poll_seconds - (time.monotonic() - started)))


def _tags_json(tags: dict) -> str:
    return json.dumps(tags, sort_keys=True)
