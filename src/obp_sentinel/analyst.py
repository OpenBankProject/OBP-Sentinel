"""Runs the analyst (`.claude/agents/obp-sentinel-analyst.md`) headless with Claude Code, on a schedule.

Once when Sentinel starts, then every `analyse_minutes`, but only when it is worth it: enough hours watched,
and something new since the last run (a new or rising signature, feedback from a person) or the last run is a
day old. Otherwise the check is recorded and no tokens are spent.

Each run is `claude -p --agent obp-sentinel-analyst --output-format stream-json`. Its steps (tool calls and
short notes, never tool results) are stored in `analysis_steps` so the web page can show what it is doing.
The tools it may use are listed explicitly: `uv run sentinel ...`, read-only git, reading files, and writing
under `work/`. Only one run happens at a time, across processes.
"""

import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from .config import Config
from .logs import about, for_instance
from .store import Store, now
from .summary import watched_seconds

logger = logging.getLogger(__name__)

AGENT = "obp-sentinel-analyst"
AGENT_FILE = Path(".claude/agents") / f"{AGENT}.md"
CHECK_EVERY_SECONDS = 60
RERUN_ANYWAY_SECONDS = 86400
RISING_FACTOR = 2
RISING_MIN_COUNT = 5
STEP_TEXT_MAX = 300
GIT_READ_COMMANDS = ("log", "show", "blame", "diff", "rev-parse")


def unavailable_reason(config: Config, scheduled: bool = True) -> str | None:
    """Why the analyst cannot run (on its schedule, if `scheduled`) from here, or None if it can."""
    if scheduled and config.analyse_minutes <= 0:
        return "Turned off (SENTINEL_ANALYSE_MINUTES=0)"
    if not shutil.which(config.claude_command):
        return f"Claude Code ({config.claude_command}) is not installed or not on PATH"
    if not AGENT_FILE.exists():
        return f"{AGENT_FILE} not found: start Sentinel from the OBP-Sentinel directory"
    if not config.obp_api_source or not Path(config.obp_api_source).is_dir():
        return "OBP_API_SOURCE is not set to the OBP-API checkout"
    return None


def whats_new(store: Store, since: int) -> list[str]:
    """Reasons to analyse again: what changed since `since` (the start of the last completed run)."""
    reasons = []
    new = store.db.execute(
        "SELECT COUNT(*) FROM signatures WHERE status = 'active' AND first_seen > ?", (since,)
    ).fetchone()[0]
    if new:
        reasons.append(f"{new} new problem{'s' if new != 1 else ''}")
    span = max(now() - since, 3600)
    rising = store.db.execute(
        """SELECT COUNT(*) FROM (
             SELECT o.signature_id,
                    SUM(CASE WHEN o.bucket_start >= :since THEN o.count ELSE 0 END) AS recent,
                    SUM(CASE WHEN o.bucket_start < :since THEN o.count ELSE 0 END) AS before
             FROM observations o JOIN signatures s ON s.id = o.signature_id
             WHERE s.status = 'active' AND s.first_seen <= :since AND o.bucket_start >= :from
             GROUP BY o.signature_id)
           WHERE recent >= :min AND recent >= :factor * before""",
        {"since": since, "from": since - span, "min": RISING_MIN_COUNT, "factor": RISING_FACTOR},
    ).fetchone()[0]
    if rising:
        reasons.append(f"{rising} rising")
    feedback = store.db.execute("SELECT COUNT(*) FROM feedback WHERE ts > ?", (since,)).fetchone()[0]
    if feedback:
        reasons.append(f"{feedback} response{'s' if feedback != 1 else ''} from people")
    return reasons


def decide(store: Store, config: Config) -> tuple[bool, str]:
    """Whether to run now, and why (or why not), in words for the web page."""
    ts = now()
    watched = watched_seconds(store, ts - 6 * 3600, ts, config.poll_seconds) / 3600
    if watched < config.analyse_min_watch_hours:
        return False, f"Waiting for data: {watched:.1f} of {config.analyse_min_watch_hours:g}h watched"
    last = store.db.execute("SELECT MAX(started_at) FROM analysis_runs WHERE status = 'done'").fetchone()[0]
    if last is None:
        return True, "First analysis"
    if ts - last >= RERUN_ANYWAY_SECONDS:
        return True, "Last analysis was a day ago"
    reasons = whats_new(store, last)
    if not reasons:
        return False, "Nothing new since the last analysis"
    return True, ", ".join(reasons)


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def claim_run(store: Store, trigger: str) -> int | None:
    """Record a new run, unless one is already going (in any process). Returns its id, or None."""
    store.db.execute("BEGIN IMMEDIATE")
    try:
        for r in store.db.execute("SELECT id, pid FROM analysis_runs WHERE status = 'running'").fetchall():
            if _pid_alive(r["pid"]):
                store.db.execute("ROLLBACK")
                return None
            store.db.execute(
                "UPDATE analysis_runs SET status = 'failed', ended_at = ?, error = 'Interrupted: Sentinel stopped' "
                "WHERE id = ?", (now(), r["id"]),
            )
        cur = store.db.execute(
            "INSERT INTO analysis_runs (started_at, trigger, status, pid) VALUES (?, ?, 'running', ?)",
            (now(), trigger, os.getpid()),
        )
        store.db.execute("COMMIT")
        return cur.lastrowid
    except BaseException:
        store.db.execute("ROLLBACK")
        raise


def command(config: Config) -> list[str]:
    source = str(Path(config.obp_api_source).resolve())
    tools = ["Read", "Grep", "Glob", "Edit(work/**)", "Bash(uv run sentinel *)"]
    tools += [f"Bash(git -C {source} {c} *)" for c in GIT_READ_COMMANDS]
    prompt = (
        f"Run the Sentinel analysis of the OBP-API instance `{config.name}` ({config.obp_base_url}); every "
        f"`uv run sentinel` command already reads that instance's data. The OBP-API source is at {source}; use that path literally "
        f"(e.g. git -C {source} log ...). At least {config.analyse_min_watch_hours:g}h of coverage is enough to go ahead."
    )
    return [
        config.claude_command, "-p", prompt, "--agent", AGENT,
        "--output-format", "stream-json", "--verbose",
        "--permission-mode", "dontAsk", "--allowedTools", *tools, "--add-dir", source,
        "--strict-mcp-config",  # none of the user's MCP servers: the analyst needs only the tools above
        "--max-budget-usd", f"{config.analyse_budget_usd:g}",
    ]


def describe(event: dict) -> list[tuple[str, str]]:
    """(kind, text) steps worth showing for one stream-json event. Tool results are never stored."""
    if event.get("type") == "system" and event.get("subtype") == "thinking_tokens":
        return [("thinking", "Thinking")]
    if event.get("type") != "assistant":
        return []
    steps = []
    for block in event.get("message", {}).get("content", []):
        if block.get("type") == "text" and block.get("text", "").strip():
            steps.append(("note", block["text"].strip()))
        elif block.get("type") == "tool_use":
            name, args = block.get("name", "?"), block.get("input", {})
            detail = {
                "Bash": args.get("command"),
                "Read": args.get("file_path"),
                "Write": args.get("file_path"),
                "Edit": args.get("file_path"),
                "Grep": " in ".join(filter(None, [args.get("pattern"), args.get("path")])),
                "Glob": args.get("pattern"),
            }.get(name) or json.dumps(args)
            steps.append(("tool", f"{name}: {detail}"))
    return [(kind, text[:STEP_TEXT_MAX]) for kind, text in steps]


def run_claude(cmd: list[str], env: dict, timeout_minutes: int, on_step, stopped: threading.Event,
               set_process=lambda process: None) -> tuple[str | None, float | None, str | None]:
    """Run headless Claude Code to the end, passing each step to `on_step(kind, text)`.

    Returns (error, cost in USD, result text); error is None when it succeeded. `set_process` is told the
    process while it runs (and None after), so it can be stopped from another thread."""
    error, cost, result = None, None, None
    timed_out = threading.Event()
    try:
        with tempfile.TemporaryFile("w+") as stderr:
            process = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=stderr,
                text=True, start_new_session=True, env={**os.environ, **env},
            )
            set_process(process)
            timer = threading.Timer(timeout_minutes * 60, lambda: (timed_out.set(), process.kill()))
            timer.start()
            try:
                for line in process.stdout:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if event.get("type") == "result":
                        cost = event.get("total_cost_usd")
                        result = (event.get("result") or "")[:2000]
                        if event.get("is_error") or event.get("subtype") != "success":
                            error = f"Claude Code stopped: {event.get('subtype')}"
                        continue
                    for kind, text in describe(event):
                        on_step(kind, text)
                code = process.wait()
            finally:
                timer.cancel()
                set_process(None)
            stderr.seek(0)
            stderr_text = stderr.read().strip()
        if timed_out.is_set():
            error = f"Timed out after {timeout_minutes} minutes"
        elif stopped.is_set():
            error = "Interrupted: stopped"
        elif code != 0 and not error:
            error = (stderr_text or f"Claude Code exited with {code}")[:500]
    except OSError as e:
        error = f"Could not start Claude Code: {e}"[:500]
    return error, cost, result


class Analyst:
    def __init__(self, config: Config):
        self.config = config
        self.process: subprocess.Popen | None = None
        self._stop = threading.Event()

    def run(self, trigger: str, background: bool = False) -> int | None:
        """Run the analyst once, now (in a thread, if `background`). Returns the run id, or None if another run is going."""
        with about(self.config.obp_base_url):
            return self._run(trigger, background)

    def _run(self, trigger: str, background: bool) -> int | None:
        store = Store(self.config.db_path)
        try:
            run_id = claim_run(store, trigger)
        finally:
            store.close()
        if run_id is None:
            logger.info("Analysis already running elsewhere; not starting another")
            return None
        logger.info("Analysis #%s started (%s)", run_id, trigger)
        if background:
            for_instance(threading.Thread(target=self._execute_in_own_store, args=(run_id,),
                                          name=f"analysis-{self.config.name}", daemon=True),
                         self.config.obp_base_url).start()
        else:
            self._execute_in_own_store(run_id)
        return run_id

    def _execute_in_own_store(self, run_id: int) -> None:
        store = Store(self.config.db_path)  # opened in the thread that uses it
        try:
            self._execute(store, run_id)
        finally:
            store.close()

    def _execute(self, store: Store, run_id: int) -> None:
        def add_step(kind: str, text: str) -> None:
            last = store.db.execute(
                "SELECT kind FROM analysis_steps WHERE run_id = ? ORDER BY id DESC LIMIT 1", (run_id,)
            ).fetchone()
            if kind == "thinking" and last and last["kind"] == "thinking":
                return
            store.db.execute(
                "INSERT INTO analysis_steps (run_id, ts, kind, text) VALUES (?, ?, ?, ?)", (run_id, now(), kind, text)
            )
            store.commit()

        status, error, cost, result = "failed", None, None, None
        try:
            error, cost, result = run_claude(
                command(self.config), {"SENTINEL_INSTANCE": self.config.name}, self.config.analyse_timeout_minutes,
                add_step, self._stop, self._set_process)
            status = "failed" if error else "done"
        finally:
            store.db.execute(
                "UPDATE analysis_runs SET status = ?, ended_at = ?, cost_usd = ?, result = ?, error = ? WHERE id = ?",
                (status, now(), cost, result, error, run_id),
            )
            store.commit()
            logger.info("Analysis #%s %s%s", run_id, status, f": {error}" if error else "")

    def _set_process(self, process: subprocess.Popen | None) -> None:
        self.process = process

    def check(self, startup: bool) -> None:
        """Decide whether to run now; run if so. Records the decision for the web page."""
        store = Store(self.config.db_path)
        try:
            go, reason = decide(store, self.config)
            store.set_state("analysis_check", {"ts": now(), "reason": reason, "ran": go})
            store.commit()
        finally:
            store.close()
        if go:
            self.run(("At startup: " if startup else "") + reason)

    def schedule_forever(self) -> None:
        """At startup, then every `analyse_minutes`. While waiting for enough data, look again every minute."""
        interval = self.config.analyse_minutes * 60
        due, startup = time.time(), True
        while not self._stop.is_set():
            if time.time() >= due:
                try:
                    self.check(startup)
                except Exception:
                    logger.exception("Analysis check failed")
                store = Store(self.config.db_path)
                try:
                    check = store.get_state("analysis_check", {})
                    waiting = check.get("reason", "").startswith("Waiting for data")
                    store.set_state("analysis_next", None if waiting else int(time.time()) + interval)
                    store.commit()
                finally:
                    store.close()
                due = time.time() + (CHECK_EVERY_SECONDS if waiting else interval)
                startup = False
            self._stop.wait(CHECK_EVERY_SECONDS)

    def start(self) -> threading.Thread:
        thread = for_instance(threading.Thread(target=self.schedule_forever, name=f"analyst-{self.config.name}",
                                               daemon=True), self.config.obp_base_url)
        thread.start()
        return thread

    def stop(self) -> None:
        self._stop.set()
        process = self.process
        if process and process.poll() is None:
            process.terminate()


def start_scheduler(config: Config) -> Analyst | None:
    """Start the analyst's schedule in the background, or log why it cannot run."""
    with about(config.obp_base_url):
        return _start_scheduler(config)


def _start_scheduler(config: Config) -> Analyst | None:
    reason = unavailable_reason(config)
    store = Store(config.db_path)
    try:
        store.set_state("analysis_unavailable", reason)
        store.commit()
    finally:
        store.close()
    if reason:
        logger.warning("Analyst not scheduled: %s", reason)
        return None
    analyst = Analyst(config)
    analyst.start()
    logger.info("Analyst scheduled: at startup, then every %s min", config.analyse_minutes)
    return analyst
