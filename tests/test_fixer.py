from dataclasses import replace

import pytest

from obp_sentinel import fixer
from obp_sentinel.store import Store
from test_history import git
from test_recheck import finding


@pytest.fixture
def trees(tmp_path):
    """A person's OBP-API checkout, and Sentinel's own worktree of it."""
    source = tmp_path / "OBP-API"
    (source / "obp-api/src").mkdir(parents=True)
    git(source, "init", "-q")
    git(source, "config", "user.name", "Ada")
    git(source, "config", "user.email", "ada@example.com")
    (source / "obp-api/src/A.scala").write_text("broken\n")
    (source / "obp-api/src/B.scala").write_text("mine\n")
    git(source, "add", ".")
    git(source, "commit", "-qm", "first")
    worktree = tmp_path / "OBP-API-sentinel-fixes"
    git(source, "worktree", "add", "-q", "--detach", str(worktree))
    return source, worktree


@pytest.fixture
def cfg(config, trees):
    source, worktree = trees
    return replace(config, obp_api_source=str(source), fix_worktree=str(worktree))


def claude_that(worktree, answer, change=True):
    def run(*args, **kwargs):
        if change:
            (worktree / "obp-api/src/A.scala").write_text("fixed\n")
        args[3]("tool", f"Edit: {worktree}/obp-api/src/A.scala")
        args[3]("result", "The file has been updated.")
        return None, 0.5, answer, None
    return run


FIXED = "Fixed: it checks.\nTests: passed: CustomerAddressTest, 9 tests.\nCommit: Update Customer Address checks the bank."


def new_finding(cfg):
    store = Store(cfg.db_path)
    fid = finding(store)
    store.close()
    return fid


def row(cfg, fid):
    store = Store(cfg.db_path)
    try:
        return dict(store.db.execute("SELECT * FROM findings WHERE id = ?", (fid,)).fetchone())
    finally:
        store.close()


def test_a_fix_is_staged_in_the_persons_working_tree_and_committed_with_their_message(cfg, trees, monkeypatch):
    source, worktree = trees
    fid = new_finding(cfg)
    monkeypatch.setattr(fixer, "run_claude", claude_that(worktree, FIXED))
    monkeypatch.setattr(fixer, "compiles", lambda w: None)
    assert fixer.fix(cfg, fid) == "staged"
    assert (source / "obp-api/src/A.scala").read_text() == "fixed\n"
    assert fixer.git(str(source), "diff", "--cached", "--name-only") == "obp-api/src/A.scala"  # staged: seen in the IDE
    assert fixer.git(str(worktree), "status", "--porcelain") == ""  # Sentinel's worktree is clean again
    f = row(cfg, fid)
    assert f["fix_message"] == "Fix: Update Customer Address checks the bank" and f["fix_files"] == '["obp-api/src/A.scala"]'
    store = Store(cfg.db_path)
    kinds = [s["kind"] for s in fixer.log_of(store, fid)]
    store.close()
    assert "tool" in kinds and "result" in kinds and kinds.count("sentinel") >= 3  # the full log, as it happened
    assert fixer.waiting(cfg).startswith(f"#{fid} is staged")  # the next fix waits
    with pytest.raises(ValueError):
        fixer.commit(cfg, fid, "  ")
    assert fixer.commit(cfg, fid, "Address  update checks the bank")["fix_status"] == "committed"
    assert fixer.git(str(source), "log", "-1", "--format=%s|%b|%an") == "Address update checks the bank||Ada"
    f = row(cfg, fid)
    assert f["status"] == "fixed" and f["fix_status"] == "committed" and fixer.waiting(cfg) is None


def test_discard_takes_the_fix_back_out(cfg, trees, monkeypatch):
    source, worktree = trees
    fid = new_finding(cfg)
    monkeypatch.setattr(fixer, "run_claude", claude_that(worktree, FIXED))
    monkeypatch.setattr(fixer, "compiles", lambda w: None)
    assert fixer.fix(cfg, fid) == "staged"
    assert fixer.take_back(cfg, fid) == {"fix_status": "discarded"}
    assert (source / "obp-api/src/A.scala").read_text() == "broken\n"
    assert fixer.git(str(source), "status", "--porcelain") == ""
    assert row(cfg, fid)["status"] == "open"


def test_a_fix_never_touches_a_file_the_person_is_changing(cfg, trees, monkeypatch):
    source, worktree = trees
    fid = new_finding(cfg)
    (source / "obp-api/src/A.scala").write_text("my own edit\n")
    monkeypatch.setattr(fixer, "run_claude", claude_that(worktree, FIXED))
    monkeypatch.setattr(fixer, "compiles", lambda w: None)
    assert fixer.fix(cfg, fid) == "failed"
    assert (source / "obp-api/src/A.scala").read_text() == "my own edit\n"
    assert "it does not apply to your working tree" in row(cfg, fid)["fix"]


def test_a_fix_that_does_not_compile_is_thrown_away(cfg, trees, monkeypatch):
    source, worktree = trees
    fid = new_finding(cfg)
    monkeypatch.setattr(fixer, "run_claude", claude_that(worktree, FIXED))
    monkeypatch.setattr(fixer, "compiles", lambda w: "error: not found")
    assert fixer.fix(cfg, fid) == "failed"
    assert (worktree / "obp-api/src/A.scala").read_text() == "broken\n"
    assert (source / "obp-api/src/A.scala").read_text() == "broken\n"
    assert row(cfg, fid)["status"] == "open"


def test_not_an_issue_changes_nothing(cfg, trees, monkeypatch):
    source, worktree = trees
    fid = new_finding(cfg)
    monkeypatch.setattr(fixer, "run_claude", claude_that(worktree, "Not an issue: it already checks.", change=False))
    assert fixer.fix(cfg, fid) == "skipped"
    f = row(cfg, fid)
    assert f["status"] == "open" and f["fix"] == "Not an issue: it already checks."


def test_at_the_usage_limit_the_finding_goes_back_to_the_head_of_the_queue(cfg, trees, monkeypatch):
    source, worktree = trees
    fid = new_finding(cfg)
    def limited(*args, **kwargs):
        (worktree / "obp-api/src/A.scala").write_text("half done\n")
        return "Claude Code stopped: error_during_execution", 0.0, "You've hit your limit · resets 3pm", None
    monkeypatch.setattr(fixer, "run_claude", limited)
    assert fixer.fix(cfg, fid) == "paused"
    assert (worktree / "obp-api/src/A.scala").read_text() == "broken\n"
    f = row(cfg, fid)
    assert f["fix_status"] == "queued" and f["fix"].startswith("Paused at Claude's usage limit") and f["fix_at"] == 0


def test_the_queue_waits_while_the_person_has_staged_changes(cfg, trees):
    source, _ = trees
    (source / "obp-api/src/B.scala").write_text("mine, changed\n")
    git(source, "add", ".")
    assert fixer.waiting(cfg).startswith("your working tree has staged changes")


def test_queueing_works_through_the_queue_in_the_background(cfg, monkeypatch):
    fid = new_finding(cfg)
    started = []
    monkeypatch.setattr(fixer.threading, "Thread",
                        lambda target, args, **kw: type("T", (), {"start": lambda self: started.append(args)})())
    assert fixer.queue(cfg, fid) == {"fix_status": "queued"}
    assert fixer.queue(cfg, fid) == {"fix_status": "queued"}  # once is enough
    assert len(started) == 1
    fixer._workers.clear()


def test_the_claude_command_may_only_edit_the_worktree_source(config, cfg, trees):
    _, worktree = trees
    cmd = fixer.command(cfg, "prompt")
    tools = cmd[cmd.index("--allowedTools") + 1:cmd.index("--add-dir")]
    assert f"Edit(/{worktree}/obp-api/src/**)" in tools
    assert not any(t.startswith("Bash(git") and (" commit" in t or " push" in t) for t in tools)
    assert fixer.unavailable_reason(replace(config, fix_worktree="")) is not None
    assert fixer.unavailable_reason(cfg) is None


def test_a_commit_message_is_one_short_line_without_a_number():
    title = {"title": "PUT /obp/v3.1.0/banks/BANK_ID/customers/CUSTOMER_ID/addresses/ID overwrites any address"}
    assert fixer.commit_message("Fixed: x.\nCommit: Fix: #61 balances  need a view.", title) == "Fix: 61 balances need a view"
    assert fixer.commit_message("Fixed: x.\nCommit: Balances need a view", title) == "Fix: Balances need a view"
    assert fixer.commit_message("Fixed: x.", title) == f"Fix: {title['title']}"
    long = fixer.commit_message("Fixed: x.\nCommit: " + "word " * 60, title)
    assert len(long) <= fixer.COMMIT_MAX and "\n" not in long and long.startswith("Fix: word")


def test_the_full_log_has_tool_results_and_edits_but_the_analysts_steps_do_not():
    from obp_sentinel.analyst import describe
    edit = {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Edit", "input": {"file_path": "A.scala", "old_string": "a", "new_string": "b"}}]}}
    result = {"type": "user", "message": {"content": [
        {"type": "tool_result", "content": [{"type": "text", "text": "BUILD SUCCESS"}]}]}}
    assert describe(edit, full=True) == [("tool", "Edit: A.scala\n- a\n+ b")]
    assert describe(result, full=True) == [("result", "BUILD SUCCESS")]
    assert describe(edit) == [("tool", "Edit: A.scala")] and describe(result) == []


def test_a_fix_committed_or_taken_out_outside_sentinel_lets_the_queue_go_on(cfg, trees, monkeypatch):
    source, worktree = trees
    monkeypatch.setattr(fixer, "run_claude", claude_that(worktree, FIXED))
    monkeypatch.setattr(fixer, "compiles", lambda w: None)
    committed, taken_out = new_finding(cfg), new_finding(cfg)
    assert fixer.fix(cfg, committed) == "staged"
    git(source, "commit", "-qm", "Committed in the IDE")
    assert fixer.waiting(cfg) is None
    f = row(cfg, committed)
    assert f["fix_status"] == "committed" and f["fix_message"] == "Committed in the IDE" and f["status"] == "fixed"
    (source / "obp-api/src/A.scala").write_text("broken\n")  # back to how it was, for the next fix to change
    git(source, "commit", "-qam", "back")
    assert fixer.fix(cfg, taken_out) == "staged"
    git(source, "reset", "-q", "--hard")  # taken out with git, not Discard
    assert fixer.waiting(cfg) is None and row(cfg, taken_out)["fix_status"] == "discarded"


def test_a_fix_whose_tests_did_not_pass_is_not_staged_and_says_why(cfg, trees, monkeypatch):
    source, worktree = trees
    fid = new_finding(cfg)
    answer = ("Fixed: it checks the bank now.\nTests: did not pass: another test run held the test server's port, so "
              "CustomerTest could not start.\nCommit: Get Customer checks the bank")
    monkeypatch.setattr(fixer, "run_claude", claude_that(worktree, answer))
    monkeypatch.setattr(fixer, "compiles", lambda w: None)
    assert fixer.fix(cfg, fid) == "failed"
    assert (source / "obp-api/src/A.scala").read_text() == "broken\n"
    f = row(cfg, fid)
    assert f["fix"].startswith("Not staged, because the tests did not pass.\nFixed: it checks the bank now.\nTests: did not")
    assert "Commit:" not in f["fix"]


def test_claude_follows_the_comments_people_saved_on_the_finding(cfg, trees, monkeypatch):
    source, worktree = trees
    fid = new_finding(cfg)
    store = Store(cfg.db_path)
    store.add_feedback(fid, "comment", "Use 404, not 400", 7)
    store.add_feedback(fid, "fixed", "Fix this: committed abc on develop: x", 7)  # Sentinel's own note: not passed on
    store.db.execute("UPDATE findings SET status = 'open' WHERE id = ?", (fid,))
    store.commit()
    assert row(cfg, fid)["status"] == "open"
    store.add_feedback(fid, "comment", "Leave v4.0.0 alone", 7)
    store.commit()
    assert row(cfg, fid)["status"] == "open"  # a comment changes nothing else
    assert fixer.comments_of(store, fid) == ["Use 404, not 400", "Leave v4.0.0 alone"]
    store.close()
    prompts = []
    def run(*args, **kwargs):
        prompts.append(args[0][args[0].index("-p") + 1])
        return claude_that(worktree, "Not an issue: x.", change=False)(*args, **kwargs)
    monkeypatch.setattr(fixer, "run_claude", run)
    fixer.fix(cfg, fid)
    assert "- Use 404, not 400\n- Leave v4.0.0 alone\n" in prompts[0] and "committed abc" not in prompts[0]


def test_fix_this_without_tests_stages_a_fix_whose_tests_were_not_run(cfg, trees, monkeypatch):
    source, worktree = trees
    fid = new_finding(cfg)
    fixer._workers.add(cfg.name)  # no worker thread: the fix is made here
    fixer.queue(cfg, fid, tests=False)
    fixer._workers.clear()
    prompts = []
    answer = "Fixed: it checks.\nTests: not run: CustomerTest, a new scenario for CI.\nCommit: Fix: Make sure that it checks"
    def run(*args, **kwargs):
        prompts.append(args[0][args[0].index("-p") + 1])
        return claude_that(worktree, answer)(*args, **kwargs)
    monkeypatch.setattr(fixer, "run_claude", run)
    monkeypatch.setattr(fixer, "compiles", lambda w: None)
    assert fixer.fix(cfg, fid) == "staged"
    assert "do not run any tests" in prompts[0] and "-DwildcardSuites" not in prompts[0]
    assert "Tests: not run" in row(cfg, fid)["fix"]
