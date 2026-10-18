from dataclasses import replace

import pytest

from obp_sentinel import fixer
from obp_sentinel.store import Store
from test_history import git
from test_recheck import finding


@pytest.fixture
def worktree(tmp_path):
    repo = tmp_path / "OBP-API"
    (repo / "obp-api/src").mkdir(parents=True)
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Simon")
    git(repo, "config", "user.email", "simon@example.com")
    (repo / "obp-api/src/A.scala").write_text("broken\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "first")
    return repo


def claude_that(worktree, answer, change=True):
    def run(*args):
        if change:
            (worktree / "obp-api/src/A.scala").write_text("fixed\n")
        return None, 0.5, answer, None
    return run


def status(config, fid):
    store = Store(config.db_path)
    try:
        return dict(store.db.execute("SELECT status, fix_status, fix FROM findings WHERE id = ?", (fid,)).fetchone())
    finally:
        store.close()


def test_a_fix_that_compiles_is_committed_and_the_finding_marked_acted(config, worktree, monkeypatch):
    cfg = replace(config, fix_worktree=str(worktree))
    store = Store(cfg.db_path)
    fid = finding(store)
    store.close()
    monkeypatch.setattr(fixer, "run_claude", claude_that(
        worktree, "Fixed: it checks. Tests: none.\nCommit: Update Customer Address checks the bank."))
    monkeypatch.setattr(fixer, "compiles", lambda w: None)
    assert fixer.fix(cfg, fid) == "committed"
    log = fixer.git(str(worktree), "log", "-1", "--format=%s|%b|%an")
    assert log == "Update Customer Address checks the bank||Simon"  # one line, no body
    s = status(cfg, fid)
    assert s["status"] == "acted" and s["fix_status"] == "committed" and "Fixed: it checks" in s["fix"]


def test_a_fix_that_does_not_compile_is_thrown_away(config, worktree, monkeypatch):
    cfg = replace(config, fix_worktree=str(worktree))
    store = Store(cfg.db_path)
    fid = finding(store)
    store.close()
    monkeypatch.setattr(fixer, "run_claude", claude_that(worktree, "Fixed: it checks. Tests: none."))
    monkeypatch.setattr(fixer, "compiles", lambda w: "error: not found")
    assert fixer.fix(cfg, fid) == "failed"
    assert (worktree / "obp-api/src/A.scala").read_text() == "broken\n"
    assert fixer.git(str(worktree), "log", "--format=%s") == "first"
    assert status(cfg, fid)["status"] == "open"


def test_not_an_issue_changes_nothing(config, worktree, monkeypatch):
    cfg = replace(config, fix_worktree=str(worktree))
    store = Store(cfg.db_path)
    fid = finding(store)
    store.close()
    monkeypatch.setattr(fixer, "run_claude", claude_that(worktree, "Not an issue: it already checks.", change=False))
    assert fixer.fix(cfg, fid) == "skipped"
    s = status(cfg, fid)
    assert s["status"] == "open" and s["fix"] == "Not an issue: it already checks."


def test_queueing_works_through_the_queue_in_the_background(config, worktree, monkeypatch, tmp_path):
    cfg = replace(config, fix_worktree=str(worktree))
    store = Store(cfg.db_path)
    fid = finding(store)
    store.close()
    started = []
    monkeypatch.setattr(fixer.threading, "Thread",
                        lambda target, args, **kw: type("T", (), {"start": lambda self: started.append(args)})())
    assert fixer.queue(cfg, fid) == {"fix_status": "queued"}
    assert fixer.queue(cfg, fid) == {"fix_status": "queued"}  # once is enough
    assert len(started) == 1
    fixer._workers.clear()


def test_the_claude_command_may_only_edit_the_worktree_source(config, worktree):
    cfg = replace(config, fix_worktree=str(worktree))
    cmd = fixer.command(cfg, "prompt")
    tools = cmd[cmd.index("--allowedTools") + 1:cmd.index("--add-dir")]
    assert f"Edit(/{worktree}/obp-api/src/**)" in tools
    assert not any(t.startswith("Bash(git") and (" commit" in t or " push" in t) for t in tools)
    assert fixer.unavailable_reason(replace(config, fix_worktree="")) is not None
    assert fixer.unavailable_reason(cfg) is None


def test_at_the_usage_limit_the_finding_goes_back_to_the_head_of_the_queue(config, worktree, monkeypatch):
    cfg = replace(config, fix_worktree=str(worktree))
    store = Store(cfg.db_path)
    fid = finding(store)
    store.close()
    def limited(*args):
        (worktree / "obp-api/src/A.scala").write_text("half done\n")
        return "Claude Code stopped: error_during_execution", 0.0, "You've hit your limit · resets 3pm", None
    monkeypatch.setattr(fixer, "run_claude", limited)
    assert fixer.fix(cfg, fid) == "paused"
    assert (worktree / "obp-api/src/A.scala").read_text() == "broken\n"
    store = Store(cfg.db_path)
    row = store.db.execute("SELECT fix_status, fix, fix_at FROM findings WHERE id = ?", (fid,)).fetchone()
    store.close()
    assert row["fix_status"] == "queued" and row["fix"].startswith("Paused at Claude's usage limit") and row["fix_at"] == 0


def test_a_commit_message_is_one_short_line_without_a_number():
    title = {"title": "PUT /obp/v3.1.0/banks/BANK_ID/customers/CUSTOMER_ID/addresses/ID overwrites any address"}
    assert fixer.commit_message("Fixed: x.\nCommit: Fix #61 balances  need a view.", title) == "Fix 61 balances need a view"
    long = fixer.commit_message("Fixed: x.", title)
    assert len(long) <= fixer.COMMIT_MAX and "\n" not in long and long.startswith("Fix: PUT")
