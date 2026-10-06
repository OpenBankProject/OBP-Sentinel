import json
import os
import stat
import threading
import time
from dataclasses import replace
from http.client import HTTPConnection

import pytest

from obp_sentinel.analyst import AGENT_FILE, Analyst, claim_run, command, decide, describe, unavailable_reason
from obp_sentinel.signatures import parse_line, signature_of
from obp_sentinel.store import Store, now
from obp_sentinel.web import analysis, make_server

EVENTS = [
    {"type": "system", "subtype": "init"},
    {"type": "system", "subtype": "thinking_tokens"},
    {"type": "system", "subtype": "thinking_tokens"},
    {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash",
                                                   "input": {"command": "uv run sentinel status"}}]}},
    {"type": "user", "message": {"content": [{"type": "tool_result", "content": "SECRET LOG TEXT"}]}},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "Nothing worth suggesting."}]}},
    {"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.42, "result": "No findings."},
]


@pytest.fixture
def analyst_config(config, tmp_path):
    """A fake `claude` that prints EVENTS as stream-json, an agent file and an OBP-API source directory."""
    (tmp_path / "events.jsonl").write_text("\n".join(json.dumps(e) for e in EVENTS) + "\n")
    fake = tmp_path / "claude"
    fake.write_text(f"#!/bin/sh\necho \"$@\" > {tmp_path}/args\ncat {tmp_path}/events.jsonl\nexit ${{FAKE_EXIT:-0}}\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    AGENT_FILE.parent.mkdir(parents=True)
    AGENT_FILE.write_text("agent")
    (tmp_path / "OBP-API").mkdir()
    return replace(config, claude_command=str(fake), obp_api_source=str(tmp_path / "OBP-API"),
                   analyse_minutes=60, analyse_min_watch_hours=2, analyse_budget_usd=2, analyse_timeout_minutes=5)


def watch(store: Store, hours: float, poll_seconds=120) -> None:
    ts = now()
    for t in range(ts - int(hours * 3600), ts + 1, poll_seconds):
        store.db.execute("INSERT INTO polls (ts, source, ok) VALUES (?, 'log:error', 1)", (t,))
    store.commit()


def add_signature(store: Store, line: str, ts: int, times=1) -> str:
    sig = signature_of("error", parse_line(line))
    for _ in range(times):
        store.record_occurrence(sig, ts, ts - ts % 900, line)
    store.commit()
    return sig.id


def test_waits_for_enough_watching(analyst_config):
    store = Store(analyst_config.db_path)
    watch(store, 1)
    go, reason = decide(store, analyst_config)
    assert not go and reason.startswith("Waiting for data")
    watch(store, 3)
    assert decide(store, analyst_config) == (True, "First analysis")
    store.close()


def test_skips_when_nothing_new(analyst_config):
    store = Store(analyst_config.db_path)
    watch(store, 3)
    add_signature(store, "ERROR c.Foo - old problem", now() - 7200)
    store.db.execute("INSERT INTO analysis_runs (started_at, trigger, status) VALUES (?, 'x', 'done')", (now() - 600,))
    store.commit()
    assert decide(store, analyst_config) == (False, "Nothing new since the last analysis")
    add_signature(store, "ERROR c.Bar - a new problem", now())
    go, reason = decide(store, analyst_config)
    assert go and "1 new problem" in reason
    store.close()


def test_describe_keeps_tool_calls_and_notes_never_results():
    steps = [step for e in EVENTS for step in describe(e)]
    assert ("tool", "Bash: uv run sentinel status") in steps
    assert ("note", "Nothing worth suggesting.") in steps
    assert not any("SECRET" in text for _, text in steps)


def test_command_limits_tools(analyst_config):
    args = command(analyst_config)
    tools = args[args.index("--allowedTools") + 1 : args.index("--add-dir")]
    assert "Edit(work/**)" in tools and "Bash(uv run sentinel *)" in tools
    assert not any(t in ("Bash", "Edit", "Write") for t in tools)
    assert all(" push" not in t and " commit" not in t for t in tools)
    assert args[args.index("--max-budget-usd") + 1] == "2"


def test_run_records_steps_and_cost(analyst_config):
    run_id = Analyst(analyst_config).run("test")
    store = Store(analyst_config.db_path)
    run = store.db.execute("SELECT * FROM analysis_runs WHERE id = ?", (run_id,)).fetchone()
    assert run["status"] == "done" and run["cost_usd"] == 0.42 and run["result"] == "No findings."
    kinds = [r["kind"] for r in store.db.execute("SELECT kind FROM analysis_steps ORDER BY id")]
    assert kinds == ["thinking", "tool", "note"]  # consecutive thinking events are one step
    view = analysis(store, analyst_config)
    assert view["runs"][0]["steps_total"] == 2 and view["runs"][0]["steps"][1]["text"].startswith("Bash:")
    store.close()


def test_failed_run_is_recorded(analyst_config, monkeypatch):
    monkeypatch.setenv("FAKE_EXIT", "1")
    run_id = Analyst(analyst_config).run("test")
    store = Store(analyst_config.db_path)
    run = store.db.execute("SELECT * FROM analysis_runs WHERE id = ?", (run_id,)).fetchone()
    assert run["status"] == "failed" and "exited with 1" in run["error"]
    store.close()


def test_one_run_at_a_time(analyst_config):
    store = Store(analyst_config.db_path)
    assert claim_run(store, "first") is not None
    assert claim_run(store, "second") is None  # this process is alive and running one
    store.db.execute("UPDATE analysis_runs SET pid = 999999999")
    store.commit()
    assert claim_run(store, "third") is not None  # the stale one is marked failed
    statuses = [r[0] for r in store.db.execute("SELECT status FROM analysis_runs ORDER BY id")]
    assert statuses == ["failed", "running"]
    store.close()


def test_unavailable_reasons(analyst_config):
    assert unavailable_reason(analyst_config) is None
    assert "Turned off" in unavailable_reason(replace(analyst_config, analyse_minutes=0))
    assert unavailable_reason(replace(analyst_config, analyse_minutes=0), scheduled=False) is None
    assert "not installed" in unavailable_reason(replace(analyst_config, claude_command="no-such-claude-xyz"))
    os.remove(AGENT_FILE)
    assert "not found" in unavailable_reason(analyst_config)


def test_page_starts_a_run_whatever_the_schedule_says(analyst_config):
    server = make_server(analyst_config, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        conn = HTTPConnection("127.0.0.1", server.server_address[1])
        conn.request("POST", "/api/analysis/run", body="{}", headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        run_id = json.loads(response.read())["run_id"]
        assert response.status == 200
        store = Store(analyst_config.db_path)
        for _ in range(100):
            run = store.db.execute("SELECT * FROM analysis_runs WHERE id = ?", (run_id,)).fetchone()
            if run["status"] != "running":
                break
            time.sleep(0.05)
        assert run["status"] == "done" and run["trigger"].startswith("By hand, from the page: Waiting for data")
        store.close()
    finally:
        server.shutdown()
        server.server_close()
