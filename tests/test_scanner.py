import json
import stat
import threading
from dataclasses import replace
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

import pytest

from obp_sentinel.scanner import AGENT_FILE, Scanner
from obp_sentinel.web import make_handler


@pytest.fixture
def scan_config(config, tmp_path):
    """A config whose "Claude Code" is a script that prints one stream-json run and says it is done."""
    (tmp_path / "OBP-API").mkdir()
    AGENT_FILE.parent.mkdir(parents=True, exist_ok=True)
    AGENT_FILE.write_text("agent")
    (tmp_path / ".claude/agents/obp-sentinel-analyst.md").write_text("agent")
    fake = tmp_path / "fake-claude"
    events = [{"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash",
                                                             "input": {"command": "uv run sentinel source next --n 2"}}]}},
              {"type": "result", "subtype": "success", "total_cost_usd": 0.12, "result": "Reviewed 2 endpoints, found nothing."}]
    fake.write_text("#!/bin/sh\n" + "".join(f"echo '{json.dumps(e)}'\n" for e in events))
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    return replace(config, obp_api_source=str(tmp_path / "OBP-API"), claude_command=str(fake),
                   source_db_path=str(tmp_path / "source.db"))


def test_go_and_stop_are_kept_and_a_scan_is_recorded(scan_config):
    scanner = Scanner([scan_config])
    assert scanner.unavailable() is None
    assert scanner.status()["on"] is False

    scanner.turn(True)
    assert Scanner([scan_config]).status()["on"] is True  # kept: a restarted Sentinel carries on
    scanner.scan()
    s = scanner.status()
    scan = s["scans"][0]
    assert (scan["status"], scan["cost_usd"], scan["result"]) == ("done", 0.12, "Reviewed 2 endpoints, found nothing.")
    assert scan["last_step"] == "Bash: uv run sentinel source next --n 2"
    assert s["next_at"] == scan["started_at"] + 10 * 60  # the next one 10 minutes after this one started

    scanner.turn(False)
    s = scanner.status()
    assert s["on"] is False and s["next_at"] is None


def test_a_scan_left_running_is_closed_at_start(scan_config):
    scanner = Scanner([scan_config])
    reviews = scanner._reviews()
    reviews.db.execute("INSERT INTO scans (started_at, status) VALUES (1, 'running')")
    reviews.db.commit()
    reviews.close()
    scan = Scanner([scan_config]).status()["scans"][0]
    assert scan["status"] == "failed" and "Interrupted" in scan["error"]


def post(port, path, body):
    conn = HTTPConnection("127.0.0.1", port)
    conn.request("POST", path, body=json.dumps(body), headers={"Content-Type": "application/json"})
    response = conn.getresponse()
    data = response.read()
    conn.close()
    return response.status, data


def test_go_from_the_page(scan_config):
    scanner = Scanner([scan_config])
    allowed = set()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler([scan_config], allowed, scanner=scanner))
    port = httpd.server_address[1]
    allowed.update({f"127.0.0.1:{port}"})
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        status, data = post(port, "/api/source/scanner", {"on": True})
        assert status == 200 and json.loads(data)["on"] is True
        status, _ = post(port, "/api/source/scanner", {"on": "yes"})
        assert status == 400
        status, data = post(port, "/api/source/scanner", {"on": False})
        assert status == 200 and json.loads(data)["on"] is False
    finally:
        httpd.shutdown()
        httpd.server_close()
