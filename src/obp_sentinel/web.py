"""`sentinel ui`: a small local web page to read findings and respond to them. Standard library only.

It reads and writes `sentinel.db`, and reads commit dates and authors from the OBP-API checkout with git; no calls
to OBP-API. It has no login, so it listens on
127.0.0.1 by default. Requests whose Host or Origin is not the address it serves are refused, so a web page
open in the same browser cannot use it (DNS rebinding, cross-site POSTs).
"""

import json
import logging
import re
import subprocess
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files

from .analyst import Analyst, decide, unavailable_reason
from .config import Config
from .store import VERDICTS, Store, now
from .summary import api_usage, coverage, signature_rows

logger = logging.getLogger(__name__)

OVERVIEW_HOURS = 6
STEPS_SHOWN = 40
WAITING_SHOWN = 5
WORDS = re.compile(r"[A-Za-z]{3}")
FEEDBACK_PATH = re.compile(r"^/api/findings/(\d+)/feedback$")
MAX_BODY_BYTES = 10_000
COMMIT_HASH = re.compile(r"\b(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b")  # at least one letter: not a plain number
_commits: dict[tuple[str, str], dict | None] = {}  # commits never change, so neither do their dates and authors


def commit_info(source: str | None, sha: str) -> dict | None:
    """Date (author date, epoch seconds) and author of commit `sha` in the OBP-API checkout, or None if it is not a commit there."""
    if not source:
        return None
    if (source, sha) not in _commits:
        try:
            out = subprocess.run(["git", "-C", source, "show", "-s", "--format=%at%x09%an", f"{sha}^{{commit}}", "--"],
                                 capture_output=True, text=True, timeout=5)
            ts, _, author = out.stdout.strip().partition("\t")
            _commits[(source, sha)] = {"ts": int(ts), "author": author} if out.returncode == 0 and author else None
        except (OSError, subprocess.TimeoutExpired):
            return None  # not cached: try again next time
    return _commits[(source, sha)]


def overview(store: Store, config: Config) -> dict:
    end = now()
    start = end - OVERVIEW_HOURS * 3600
    c = coverage(store, config, start, end)
    last_poll = store.db.execute("SELECT MAX(ts) FROM polls WHERE ok = 1").fetchone()[0]
    return {
        "obp_base_url": config.obp_base_url,
        "window_hours": OVERVIEW_HOURS,
        "coverage": c,
        "instance": c["instances"][-1] if c["instances"] else None,
        "api_usage": api_usage(store, start, end, start - OVERVIEW_HOURS * 3600),
        "last_poll": last_poll,
        "now": end,
        "digest_size": config.digest_size,
        "max_per_week": config.max_per_week,
        "snooze_days": config.snooze_days,
    }


def activity(store: Store, config: Config) -> dict:
    """What the collector has been doing: each source's latest poll, and per-bucket counts for the last hours."""
    end = now()
    size = config.bucket_minutes * 60
    first_bucket = end - end % size - (OVERVIEW_HOURS * 3600 // size - 1) * size
    sources = []
    for r in store.db.execute(
        """SELECT p.source, p.ts, p.ok, p.fetched, p.new, p.gap, p.error,
                  (SELECT COUNT(*) FROM polls q WHERE q.source = p.source AND q.ts > :hour) AS polls_last_hour,
                  (SELECT COUNT(*) FROM polls q WHERE q.source = p.source AND q.ts > :hour AND q.ok = 0) AS failed_last_hour,
                  (SELECT MAX(ts) FROM polls q WHERE q.source = p.source AND q.ok = 1) AS last_ok
           FROM polls p JOIN (SELECT source, MAX(id) AS id FROM polls WHERE ts > :day GROUP BY source) l ON l.id = p.id
           ORDER BY p.source""",
        {"hour": end - 3600, "day": end - 86400},
    ):
        sources.append(dict(r))
    logs = dict(store.db.execute(
        "SELECT bucket_start, SUM(count) FROM observations WHERE bucket_start >= ? GROUP BY bucket_start", (first_bucket,)
    ))
    calls = dict(store.db.execute(
        "SELECT bucket_start, count FROM metric_buckets WHERE bucket_start >= ?", (first_bucket,)
    ))
    totals = store.db.execute(
        """SELECT (SELECT COALESCE(SUM(count), 0) FROM observations WHERE bucket_start >= :hour) AS log_lines_last_hour,
                  (SELECT COUNT(DISTINCT signature_id) FROM observations WHERE bucket_start >= :hour) AS signatures_last_hour,
                  (SELECT COUNT(*) FROM signatures WHERE first_seen >= :day) AS new_signatures_last_day,
                  (SELECT COUNT(*) FROM signatures) AS signatures_total""",
        {"hour": end - end % size - 3 * size, "day": end - 86400},
    ).fetchone()
    return {
        "now": end,
        "poll_seconds": config.poll_seconds,
        "bucket_minutes": config.bucket_minutes,
        "sources": sources,
        "buckets": [
            {"start": b, "log_lines": logs.get(b, 0), "api_calls": calls.get(b)}
            for b in range(first_bucket, end + 1, size)
        ],
        "totals": dict(totals),
    }


def analysis(store: Store, config: Config) -> dict:
    """The analyst: the run going now (with its latest steps), recent runs, the schedule, and what awaits it."""
    def run_info(r) -> dict:
        run = dict(r)
        run["findings_updated"] = store.db.execute(
            "SELECT COUNT(*) FROM findings WHERE updated_at >= ? AND updated_at <= ?",
            (run["started_at"], run["ended_at"] or now()),
        ).fetchone()[0]
        run["steps_total"] = store.db.execute(
            "SELECT COUNT(*) FROM analysis_steps WHERE run_id = ? AND kind != 'thinking'", (run["id"],)
        ).fetchone()[0]
        run["steps"] = [dict(s) for s in reversed(store.db.execute(
            "SELECT ts, kind, text FROM analysis_steps WHERE run_id = ? ORDER BY id DESC LIMIT ?", (run["id"], STEPS_SHOWN)
        ).fetchall())]
        return run

    runs = store.db.execute("SELECT * FROM analysis_runs ORDER BY id DESC LIMIT 6").fetchall()
    end = now()
    start = end - OVERVIEW_HOURS * 3600
    waiting = [
        {k: r[k] for k in ("id", "level", "template", "endpoint", "window_count", "prev_count", "first_seen")}
        for r in signature_rows(store, start, end, start - OVERVIEW_HOURS * 3600, 200)
        if not r["findings"] and WORDS.search(r["template"])  # not separator lines like "====="
    ][:WAITING_SHOWN]
    return {
        "now": end,
        "interval_minutes": config.analyse_minutes,
        "budget_usd": config.analyse_budget_usd,
        "unavailable": store.get_state("analysis_unavailable"),
        "by_hand_unavailable": unavailable_reason(config, scheduled=False),
        "last_check": store.get_state("analysis_check"),
        "next_check": store.get_state("analysis_next"),
        "runs": [run_info(r) if i == 0 else dict(r) for i, r in enumerate(runs)],
        "waiting": waiting,
    }


def findings(store: Store, config: Config) -> list[dict]:
    first_suggested = dict(store.db.execute("SELECT finding_id, MIN(ts) FROM suggestions GROUP BY finding_id"))
    latest_feedback = {
        r["finding_id"]: {"ts": r["ts"], "verdict": r["verdict"], "comment": r["comment"]}
        for r in store.db.execute(
            "SELECT f.* FROM feedback f JOIN (SELECT finding_id, MAX(id) AS id FROM feedback GROUP BY finding_id) l "
            "ON l.id = f.id"
        )
    }
    result = []
    for f in store.findings():
        item = dict(f)
        item["files"] = json.loads(item["files"])
        item["signature_ids"] = json.loads(item["signature_ids"])
        item["first_suggested"] = first_suggested.get(f["id"])
        item["feedback"] = latest_feedback.get(f["id"])
        item["evidence_commits"] = {
            sha: info for sha in set(COMMIT_HASH.findall(item["evidence"] or ""))
            if (info := commit_info(config.obp_api_source, sha))
        }
        result.append(item)
    return result


def make_handler(config: Config, allowed_hosts: set[str], analyst: Analyst | None = None):
    """`analyst` runs analyses asked for on the page; pass the scheduled one so stopping Sentinel stops them too."""
    page = files("obp_sentinel").joinpath("ui.html").read_bytes()
    analyst = analyst or Analyst(config)

    class Handler(BaseHTTPRequestHandler):
        server_version = "OBP-Sentinel"

        def log_message(self, fmt, *args):
            logger.debug("%s %s", self.address_string(), fmt % args)

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' "
                "https://fonts.googleapis.com; font-src https://fonts.gstatic.com; frame-ancestors 'none'",
            )
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, value) -> None:
            self._send(status, json.dumps(value).encode(), "application/json")

        def _trusted(self) -> bool:
            if self.headers.get("Host", "") not in allowed_hosts:
                return False
            origin = self.headers.get("Origin")
            return origin is None or origin.split("://", 1)[-1] in allowed_hosts

        def _with_store(self, fn):
            store = Store(config.db_path)
            try:
                return fn(store)
            finally:
                store.close()

        def do_GET(self):
            if not self._trusted():
                return self._json(HTTPStatus.FORBIDDEN, {"error": "Unexpected Host or Origin"})
            path = self.path.split("?", 1)[0]
            if path == "/":
                return self._send(HTTPStatus.OK, page, "text/html; charset=utf-8")
            if path == "/api/overview":
                return self._json(HTTPStatus.OK, self._with_store(lambda s: overview(s, config)))
            if path == "/api/activity":
                return self._json(HTTPStatus.OK, self._with_store(lambda s: activity(s, config)))
            if path == "/api/analysis":
                return self._json(HTTPStatus.OK, self._with_store(lambda s: analysis(s, config)))
            if path == "/api/findings":
                return self._json(HTTPStatus.OK, self._with_store(lambda s: findings(s, config)))
            self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

        def do_POST(self):
            if not self._trusted():
                return self._json(HTTPStatus.FORBIDDEN, {"error": "Unexpected Host or Origin"})
            # A JSON body cannot be sent cross-site without a CORS preflight, which this server never answers
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                return self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "Send application/json"})
            if self.path == "/api/analysis/run":
                return self._run_analysis()
            match = FEEDBACK_PATH.match(self.path)
            if not match:
                return self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY_BYTES:
                return self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "Too large"})
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return self._json(HTTPStatus.BAD_REQUEST, {"error": "Invalid JSON"})
            verdict = body.get("verdict")
            comment = (body.get("comment") or "").strip() or None
            if verdict not in VERDICTS:
                return self._json(HTTPStatus.BAD_REQUEST, {"error": f"verdict must be one of {', '.join(VERDICTS)}"})

            def record(store: Store):
                store.add_feedback(int(match.group(1)), verdict, comment, config.snooze_days)
                store.commit()

            try:
                self._with_store(record)
            except ValueError as e:
                return self._json(HTTPStatus.NOT_FOUND, {"error": str(e)})
            self._json(HTTPStatus.OK, {"ok": True})

        def _run_analysis(self):
            """Start an analysis now, whatever the schedule would decide (like `sentinel analyse --force`)."""
            reason = unavailable_reason(config, scheduled=False)
            if reason:
                return self._json(HTTPStatus.CONFLICT, {"error": reason})
            why = self._with_store(lambda s: decide(s, config)[1])
            run_id = analyst.run("By hand, from the page: " + why, background=True)
            if run_id is None:
                return self._json(HTTPStatus.CONFLICT, {"error": "An analysis is already running"})
            self._json(HTTPStatus.OK, {"run_id": run_id})

    return Handler


def make_server(config: Config, host: str, port: int, extra_hosts: tuple[str, ...] = (),
                analyst: Analyst | None = None) -> ThreadingHTTPServer:
    """`extra_hosts` are other names the page is reached by (e.g. behind a proxy)."""
    allowed_hosts: set[str] = set()
    server = ThreadingHTTPServer((host, port), make_handler(config, allowed_hosts, analyst))
    port = server.server_address[1]
    names = {host, *extra_hosts} | ({"localhost", "127.0.0.1"} if host in ("127.0.0.1", "localhost") else set())
    allowed_hosts.update(f"{name}:{port}" for name in names)
    logger.info("Sentinel UI on http://%s:%s", host, port)
    return server


def serve(config: Config, host: str, port: int, extra_hosts: tuple[str, ...] = (),
          analyst: Analyst | None = None) -> None:
    """Serve until interrupted."""
    server = make_server(config, host, port, extra_hosts, analyst)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def serve_in_background(config: Config, host: str, port: int, extra_hosts: tuple[str, ...] = (),
                        analyst: Analyst | None = None) -> ThreadingHTTPServer:
    """Serve from a daemon thread, so it stops with the process. Call `shutdown()` on the result to stop sooner."""
    server = make_server(config, host, port, extra_hosts, analyst)
    threading.Thread(target=server.serve_forever, name="sentinel-ui", daemon=True).start()
    return server
