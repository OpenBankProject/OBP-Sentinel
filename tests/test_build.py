from dataclasses import replace

from obp_sentinel import build

URL = "https://github.com/ada/OBP-API/actions"
API = "https://api.github.com/repos/ada/OBP-API/actions"


def run(id, name, conclusion="success", status="completed", sha="abcdef1234567", created="2026-10-19T10:00:00Z"):
    return {"id": id, "name": name, "status": status, "conclusion": conclusion, "head_sha": sha,
            "created_at": created, "html_url": f"https://github.com/ada/OBP-API/actions/runs/{id}"}


def fake(monkeypatch, runs, jobs=()):
    asked = []
    def get(url):
        asked.append(url)
        return {"jobs": list(jobs)} if "/jobs" in url else {"workflow_runs": runs}
    monkeypatch.setattr(build, "_get", get)
    return asked


def test_ok_when_the_latest_run_of_each_workflow_passed(monkeypatch):
    asked = fake(monkeypatch, [run(3, "Build", created="2026-10-19T11:00:00Z"), run(2, "Scan", "skipped"),
                               run(1, "Build", "failure")])  # an older failure of Build no longer counts
    shown = build.check(URL, "develop")
    assert shown["state"] == "ok" and "on develop passed, up to abcdef123" in shown["text"]
    assert asked == [f"{API}/runs?per_page=30&branch=develop"]


def test_failing_names_the_workflow_commit_job_and_step(monkeypatch):
    fake(monkeypatch, [run(5, "Build", "failure")],
         [{"name": "compile", "conclusion": "success", "steps": []},
          {"name": "test (3, v6 only)", "conclusion": "failure",
           "steps": [{"name": "Set up job", "conclusion": "success"}, {"name": "Run tests", "conclusion": "failure"}]}])
    shown = build.check(URL, "develop")
    assert shown["state"] == "failing" and shown["url"].endswith("/runs/5")
    assert shown["text"] == "Build failure at abcdef123 on develop: job test (3, v6 only) failed at step Run tests"


def test_running_and_unknown(monkeypatch):
    fake(monkeypatch, [run(7, "Build", None, "in_progress")])
    assert build.check(URL, None)["state"] == "running"
    assert build.check("https://example.com/x", None)["state"] == "unknown"


def test_failure_gives_the_failed_jobs_steps_and_annotations(monkeypatch, config):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(build, "_branch", lambda source: "develop")
    def get(url, text=False):
        if "/annotations" in url:
            return [{"path": "obp-api/src/A.scala", "start_line": 12, "annotation_level": "failure",
                     "message": "not found: value x"}, {"annotation_level": "warning", "message": "deprecated"}]
        if "/jobs" in url:
            return {"jobs": [{"id": 9, "name": "compile", "conclusion": "failure",
                              "steps": [{"name": "Build", "conclusion": "failure"}]}]}
        return {"workflow_runs": [run(5, "Build", "failure")]}
    monkeypatch.setattr(build, "_get", get)
    failed = build.failure(replace(config, build_actions_url=URL))
    assert failed["run_id"] == 5 and failed["sha"] == "abcdef1234567"
    assert failed["details"] == "Job compile failed at step Build.\n- obp-api/src/A.scala:12 not found: value x"
    monkeypatch.setattr(build, "_get", lambda url, text=False: {"workflow_runs": [run(6, "Build")]})
    assert build.failure(replace(config, build_actions_url=URL)) is None
