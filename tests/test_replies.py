import json

from senti import fixer, recheck, replies
from senti.store import Store
from test_fixer import FIXED, claude_that, new_finding, row, cfg, trees  # noqa: F401 (fixtures)


def comment(cfg, fid, text):
    store = Store(cfg.db_path)
    store.add_feedback(fid, "comment", text, 7)
    store.commit()
    store.close()


def test_the_reply_and_the_action_are_read_from_the_answer():
    assert replies.parse("Reply: It is a dev script.\nNobody reaches it.\nAction: dismissed") == \
        ("It is a dev script.\nNobody reaches it.", "dismissed")
    assert replies.parse("Reply: Yes.\nAction: delete everything") == ("Yes.", "none")  # not an action Senti has
    assert replies.parse(None) == (None, "none")


def test_answers_the_latest_comment_with_the_earlier_ones_as_context_and_acts_on_it(cfg, monkeypatch):
    fid = new_finding(cfg)
    comment(cfg, fid, "Is this reachable?")
    comment(cfg, fid, "It is a developer script, please dismiss it")
    seen = []
    monkeypatch.setattr(replies, "run_claude", lambda cmd, *a: seen.append(cmd[2]) or (
        None, 0.2, "Reply: Agreed: only developers run it by hand.\nAction: dismissed", None))
    assert replies.answer(cfg, fid) == "Agreed: only developers run it by hand."
    assert "- Is this reachable?" in seen[0] and "The comment to answer:\nIt is a developer script" in seen[0]
    f = row(cfg, fid)
    assert f["status"] == "dismissed" and f["reply_status"] == "done"
    assert f["reply"] == "Agreed: only developers run it by hand.\n\nDismissed it."
    assert f["reply_to"] == "It is a developer script, please dismiss it"
    store = Store(cfg.db_path)
    assert store.db.execute("SELECT from_sentinel FROM feedback WHERE verdict = 'dismissed'").fetchone()[0] == 0  # the person asked


def test_fix_again_takes_a_staged_fix_back_out_and_queues_it(cfg, trees, monkeypatch):
    source, worktree = trees
    fid = new_finding(cfg)
    monkeypatch.setattr(fixer, "run_claude", claude_that(worktree, FIXED))
    monkeypatch.setattr(fixer, "compiles", lambda w: None)
    assert fixer.fix(cfg, fid) == "staged"
    monkeypatch.setattr(fixer, "start", lambda config: None)  # the queue is not worked through here
    said = replies.act(cfg, fid, "fix again without tests")
    assert said.startswith("Took the staged fix back out of your working tree and queued it for Fix this without tests")
    f = row(cfg, fid)
    assert f["fix_status"] == "queued" and f["fix_tests"] == 0
    assert (source / "obp-api/src/A.scala").read_text() == "broken\n"


def test_a_failed_answer_says_so_and_does_nothing(cfg):
    fid = new_finding(cfg)
    comment(cfg, fid, "Dismiss it")
    assert replies.answer(cfg, fid) is None  # conftest: Claude Code is not run in tests
    f = row(cfg, fid)
    assert f["reply_status"] == "failed" and f["reply"].startswith("Could not answer") and f["status"] == "open"
