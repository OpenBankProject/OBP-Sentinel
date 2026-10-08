"""The source scanner: reviews the code of a few endpoints every SENTINEL_SCAN_MINUTES, while it is on.

It is turned on and off with Go and Stop on the web page. Whether it is on is kept in the source review's
database, so it carries on after a restart. Each scan is headless Claude Code running the agent
`.claude/agents/obp-sentinel-source-reviewer.md`: it takes the next endpoints from `sentinel source next`,
reads their code, imports what it finds as findings and records what it read. Only one scan runs at a time.
One scanner serves all instances, like the source review itself.
"""

import logging
import threading
import time
from pathlib import Path

from .analyst import GIT_READ_COMMANDS, run_claude, unavailable_reason
from .config import Config
from .source import Reviews, open_reviews
from .store import now

logger = logging.getLogger(__name__)

AGENT = "obp-sentinel-source-reviewer"
AGENT_FILE = Path(".claude/agents") / f"{AGENT}.md"
TICK_SECONDS = 5

SCHEMA = """
CREATE TABLE IF NOT EXISTS scanner (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    on_     INTEGER NOT NULL DEFAULT 0,   -- Go pressed (1) or Stop (0)
    changed INTEGER                        -- when it was last turned on or off
);
INSERT OR IGNORE INTO scanner (id, on_) VALUES (1, 0);
CREATE TABLE IF NOT EXISTS scans (
    id         INTEGER PRIMARY KEY,
    started_at INTEGER NOT NULL,
    ended_at   INTEGER,
    status     TEXT NOT NULL,             -- running | done | failed
    cost_usd   REAL,
    result     TEXT,
    error      TEXT,
    last_step  TEXT                        -- what it is doing (or did last)
);
"""


def command(config: Config) -> list[str]:
    source = str(Path(config.obp_api_source).resolve())
    tools = ["Read", "Grep", "Glob", "Edit(work/**)", "Bash(uv run sentinel *)"]
    tools += [f"Bash(git -C {source} {c} *)" for c in GIT_READ_COMMANDS]
    prompt = (f"Review the next {config.scan_endpoints} endpoints of the OBP-API source review. The OBP-API "
              f"source is at {source}; use that path literally (e.g. git -C {source} show ...).")
    return [
        config.claude_command, "-p", prompt, "--agent", AGENT,
        "--output-format", "stream-json", "--verbose",
        "--permission-mode", "dontAsk", "--allowedTools", *tools, "--add-dir", source,
        "--strict-mcp-config",
        "--max-budget-usd", f"{config.scan_budget_usd:g}",
    ]


class Scanner:
    def __init__(self, configs: list[Config]):
        self.config = configs[0]  # the scanner's settings, like the page's, are not per instance
        self.process = None
        self._stop = threading.Event()  # Sentinel stopping
        self._cancel = threading.Event()  # Stop pressed during a scan
        self._lock = threading.Lock()
        open_reviews(self.config).close()  # read back reviews kept in SENTINEL_REVIEW_DIR, if the database lacks them
        reviews = self._reviews()
        try:
            # A scan left running by a Sentinel that stopped
            reviews.db.execute("UPDATE scans SET status = 'failed', ended_at = ?, error = 'Interrupted: Sentinel stopped' "
                               "WHERE status = 'running'", (now(),))
            reviews.db.commit()
        finally:
            reviews.close()

    def _reviews(self) -> Reviews:
        reviews = Reviews(self.config.source_db_path)
        reviews.db.executescript(SCHEMA)
        return reviews

    def unavailable(self) -> str | None:
        reason = unavailable_reason(self.config, scheduled=False)
        if reason:
            return reason
        if not AGENT_FILE.exists():
            return f"{AGENT_FILE} not found: start Sentinel from the OBP-Sentinel directory"
        return None

    def status(self) -> dict:
        reviews = self._reviews()
        try:
            on, changed = reviews.db.execute("SELECT on_, changed FROM scanner WHERE id = 1").fetchone()
            scans = [dict(r) for r in reviews.db.execute("SELECT * FROM scans ORDER BY id DESC LIMIT 6")]
        finally:
            reviews.close()
        running = bool(scans) and scans[0]["status"] == "running"
        last_start = scans[0]["started_at"] if scans else None
        next_at = None
        if on and not running:
            next_at = max(now(), (last_start or 0) + self.config.scan_minutes * 60)
        return {"on": bool(on), "changed": changed, "running": running, "next_at": next_at, "scans": scans,
                "every_minutes": self.config.scan_minutes, "budget_usd": self.config.scan_budget_usd,
                "endpoints_per_scan": self.config.scan_endpoints, "unavailable": self.unavailable()}

    def turn(self, on: bool) -> None:
        """Go (on) or Stop (off). Stop also ends a scan that is running."""
        reviews = self._reviews()
        try:
            reviews.db.execute("UPDATE scanner SET on_ = ?, changed = ? WHERE id = 1", (int(on), now()))
            reviews.db.commit()
        finally:
            reviews.close()
        logger.info("Source scanner %s", "on" if on else "off")
        if not on:
            self._cancel.set()
            process = self.process
            if process and process.poll() is None:
                process.terminate()

    def scan(self) -> None:
        """One scan, now, unless one is running."""
        if not self._lock.acquire(blocking=False):
            return
        try:
            self._cancel.clear()
            reviews = self._reviews()
            try:
                scan_id = reviews.db.execute("INSERT INTO scans (started_at, status) VALUES (?, 'running')",
                                             (now(),)).lastrowid
                reviews.db.commit()

                def on_step(kind: str, text: str) -> None:
                    if kind != "thinking":
                        reviews.db.execute("UPDATE scans SET last_step = ? WHERE id = ?", (text, scan_id))
                        reviews.db.commit()

                logger.info("Source scan #%s started", scan_id)
                error, cost, result = run_claude(command(self.config), {}, self.config.analyse_timeout_minutes,
                                                 on_step, self._cancel, self._set_process)
                if error and self._cancel.is_set():
                    error = "Stopped from the page"
                reviews.db.execute("UPDATE scans SET status = ?, ended_at = ?, cost_usd = ?, result = ?, error = ? "
                                   "WHERE id = ?", ("failed" if error else "done", now(), cost, result, error, scan_id))
                reviews.db.commit()
                logger.info("Source scan #%s %s", scan_id, f"failed: {error}" if error else "done")
            finally:
                reviews.close()
        finally:
            self._lock.release()

    def _set_process(self, process) -> None:
        self.process = process

    def schedule_forever(self) -> None:
        while not self._stop.is_set():
            try:
                s = self.status()
                if s["on"] and not s["running"] and not s["unavailable"] and s["next_at"] <= now():
                    self.scan()
            except Exception:
                logger.exception("Source scan failed")
            self._stop.wait(TICK_SECONDS)

    def start(self) -> None:
        threading.Thread(target=self.schedule_forever, name="source-scanner", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self._cancel.set()
        process = self.process
        if process and process.poll() is None:
            process.terminate()
