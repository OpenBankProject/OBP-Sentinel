import json
import os
import subprocess
import threading
from dataclasses import replace
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

import pytest

from obp_sentinel.store import Store
from obp_sentinel.web import findings, make_handler


def finding(key, priority_inputs=(4, 0.8, 1, "rising")):
    impact, confidence, effort, trend = priority_inputs
    return {
        "key": key, "title": f"Problem {key}", "category": "bug", "signature_ids": ["s1"],
        "evidence": "<img src=x onerror=alert(1)>", "hypothesis": "h", "files": ["a.scala:1"], "suggested_fix": "f",
        "impact": impact, "confidence": confidence, "effort": effort, "trend": trend,
    }


@pytest.fixture
def server(config):
    store = Store(config.db_path)
    store.upsert_finding(finding("one"))
    store.commit()
    store.close()
    allowed = set()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler([config], allowed))
    port = httpd.server_address[1]
    allowed.update({f"127.0.0.1:{port}", f"localhost:{port}"})
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield port
    httpd.shutdown()
    httpd.server_close()


def request(port, method, path, body=None, headers=None):
    conn = HTTPConnection("127.0.0.1", port)
    conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers or {})
    response = conn.getresponse()
    data = response.read()
    conn.close()
    return response.status, data


def test_page_and_findings(server):
    status, page = request(server, "GET", "/")
    assert status == 200 and b"<title>Sentinel</title>" in page
    status, data = request(server, "GET", "/api/findings")
    items = json.loads(data)
    assert status == 200 and items[0]["key"] == "one" and items[0]["files"] == ["a.scala:1"]
    status, data = request(server, "GET", "/api/overview")
    assert status == 200 and "api_usage" in json.loads(data)
    status, data = request(server, "GET", "/api/source")
    assert status == 200 and json.loads(data) == {"total": 0, "reviewed": 0, "files": 0, "functions": 0, "tiers": [], "recent": [],
                                                  "instances": [{"name": "default", "listed_at": None}]}


def test_feedback_is_recorded(server, config):
    status, _ = request(server, "POST", "/api/findings/1/feedback", {"verdict": "later", "comment": "next sprint"},
                        {"Content-Type": "application/json"})
    assert status == 200
    store = Store(config.db_path)
    f = store.findings()[0]
    assert f["status"] == "later" and f["snoozed_until"]
    assert store.db.execute("SELECT comment FROM feedback").fetchone()[0] == "next sprint"
    store.close()


def test_bad_feedback_is_refused(server):
    json_headers = {"Content-Type": "application/json"}
    assert request(server, "POST", "/api/findings/1/feedback", {"verdict": "maybe"}, json_headers)[0] == 400
    assert request(server, "POST", "/api/findings/99/feedback", {"verdict": "fixed"}, json_headers)[0] == 404
    # A form post (what another site could send without a preflight) is refused
    assert request(server, "POST", "/api/findings/1/feedback", {"verdict": "fixed"},
                   {"Content-Type": "text/plain"})[0] == 415


def test_other_hosts_and_origins_are_refused(server):
    assert request(server, "GET", "/api/findings", headers={"Host": "evil.example:80"})[0] == 403
    assert request(server, "POST", "/api/findings/1/feedback", {"verdict": "dismissed"},
                   {"Content-Type": "application/json", "Origin": "https://evil.example"})[0] == 403
    assert request(server, "GET", "/api/findings", headers={"Origin": f"http://localhost:{server}"})[0] == 200


def test_activity_reports_each_source_and_buckets(server, config):
    store = Store(config.db_path)
    store.record_poll("log:error", ok=True, fetched=5, new=2)
    store.record_poll("metrics", ok=False, error="403 CanReadAggregateMetrics")
    store.commit()
    store.close()
    status, data = request(server, "GET", "/api/activity")
    a = json.loads(data)
    assert status == 200
    by_source = {s["source"]: s for s in a["sources"]}
    assert by_source["log:error"]["new"] == 2 and by_source["log:error"]["failed_last_hour"] == 0
    assert by_source["metrics"]["ok"] == 0 and by_source["metrics"]["last_ok"] is None
    assert len(a["buckets"]) == 6 * 60 // config.bucket_minutes


def test_analysis_cannot_be_started_without_the_analyst(server):
    # No agent file or OBP-API source here, so the page says why instead of starting a run
    status, data = request(server, "POST", "/api/analysis/run", {}, {"Content-Type": "application/json"})
    assert status == 409 and "not" in json.loads(data)["error"]
    assert json.loads(request(server, "GET", "/api/analysis")[1])["by_hand_unavailable"]


def test_evidence_commits_get_date_and_author(config, tmp_path):
    repo = tmp_path / "OBP-API"
    repo.mkdir()
    git = ["git", "-C", str(repo), "-c", "user.name=Ada Dev", "-c", "user.email=ada@example.com"]
    subprocess.run([*git, "init", "-q"], check=True)
    # A fixed committer date too, so the hash is always the same (one with only digits is not taken for a commit)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "x", "--date", "@1791201600 +0000"], check=True,
                   env={**os.environ, "GIT_COMMITTER_DATE": "@1791201600 +0000"})
    sha = subprocess.run([*git, "rev-parse", "--short=8", "HEAD"], capture_output=True, text=True).stdout.strip()
    store = Store(config.db_path)
    store.upsert_finding({**finding("one"), "evidence": f"Signature 3d4b07fc4b73 survived commits {sha} and abcdef12"})
    items = findings(store, replace(config, obp_api_source=str(repo)))
    store.close()
    assert items[0]["evidence_commits"] == {sha: {"ts": 1791201600, "author": "Ada Dev"}}


def test_some_action_taken_is_recorded_and_not_suggested_again(server, config):
    from obp_sentinel.digest import eligible_findings

    status, _ = request(server, "POST", "/api/findings/1/feedback", {"verdict": "acted", "comment": "added a cap"},
                        {"Content-Type": "application/json"})
    assert status == 200
    store = Store(config.db_path)
    assert store.findings()[0]["status"] == "acted" and not eligible_findings(store, config)
    store.close()


def test_each_instance_has_its_own_findings(config, tmp_path):
    other = replace(config, name="staging", db_path=str(tmp_path / "staging.db"))
    store = Store(other.db_path)
    store.upsert_finding(finding("staging-only"))
    store.commit()
    store.close()
    allowed = set()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler([config, other], allowed))
    port = httpd.server_address[1]
    allowed.add(f"127.0.0.1:{port}")
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        assert [i["name"] for i in json.loads(request(port, "GET", "/api/instances")[1])] == [config.name, "staging"]
        assert json.loads(request(port, "GET", "/api/findings")[1]) == []  # the first instance by default
        status, data = request(port, "GET", "/api/findings?instance=staging")
        assert status == 200 and json.loads(data)[0]["key"] == "staging-only"
        assert request(port, "GET", "/api/findings?instance=nope")[0] == 404
        status, _ = request(port, "POST", "/api/findings/1/feedback?instance=staging", {"verdict": "fixed"},
                            {"Content-Type": "application/json"})
        assert status == 200
        store = Store(other.db_path)
        assert store.findings()[0]["status"] == "fixed"
        store.close()
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_instances_show_running_commit_age_and_problems(server, config, monkeypatch):
    from obp_sentinel import web

    monkeypatch.setattr(web, "github_commit_info", lambda repo, sha: {"ts": 1791000000, "author": "A"} if sha == "abc1234" else None)
    store = Store(config.db_path)
    store.record_deployment("abc1234")
    store.record_poll("log:error", ok=False, error='GET http://x/obp/v5.1.0/system/log-cache/error failed (401): {"code":401}')
    store.commit()
    store.close()
    status, data = request(server, "GET", "/api/instances")
    [instance] = json.loads(data)
    assert status == 200 and instance["commit"]["commit_ts"] == 1791000000 and instance["problems"] == 1
    status, data = request(server, "GET", "/api/health")
    assert status == 200 and json.loads(data)["problems"][0]["key"] == "token-rejected"
