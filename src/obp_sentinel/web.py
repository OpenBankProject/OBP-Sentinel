"""`sentinel ui`: a small local web page to read findings and respond to them. Standard library only.

It reads and writes `sentinel.db` and nothing else: no calls to OBP-API. It has no login, so it listens on
127.0.0.1 by default. Requests whose Host or Origin is not the address it serves are refused, so a web page
open in the same browser cannot use it (DNS rebinding, cross-site POSTs).
"""

import json
import logging
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files

from .config import Config
from .store import Store, now
from .summary import api_usage, coverage

logger = logging.getLogger(__name__)

VERDICTS = ("accepted", "dismissed", "later", "fixed")
OVERVIEW_HOURS = 6
FEEDBACK_PATH = re.compile(r"^/api/findings/(\d+)/feedback$")
MAX_BODY_BYTES = 10_000


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


def findings(store: Store) -> list[dict]:
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
        result.append(item)
    return result


def make_handler(config: Config, allowed_hosts: set[str]):
    page = files("obp_sentinel").joinpath("ui.html").read_bytes()

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
            if path == "/api/findings":
                return self._json(HTTPStatus.OK, self._with_store(findings))
            self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

        def do_POST(self):
            if not self._trusted():
                return self._json(HTTPStatus.FORBIDDEN, {"error": "Unexpected Host or Origin"})
            # A JSON body cannot be sent cross-site without a CORS preflight, which this server never answers
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                return self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "Send application/json"})
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

    return Handler


def serve(config: Config, host: str, port: int, extra_hosts: tuple[str, ...] = ()) -> None:
    """Serve until interrupted. `extra_hosts` are other names the page is reached by (e.g. behind a proxy)."""
    allowed_hosts: set[str] = set()
    server = ThreadingHTTPServer((host, port), make_handler(config, allowed_hosts))
    port = server.server_address[1]
    names = {host, *extra_hosts} | ({"localhost", "127.0.0.1"} if host in ("127.0.0.1", "localhost") else set())
    allowed_hosts.update(f"{name}:{port}" for name in names)
    logger.info("Sentinel UI on http://%s:%s (Ctrl-C to stop)", host, port)
    try:
        server.serve_forever()
    finally:
        server.server_close()
