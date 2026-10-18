from dataclasses import replace

from obp_sentinel import recheck
from obp_sentinel.store import Store, now
from test_history import git, make_repo


def finding(store, files=("A.scala:2",), signature_ids=()):
    fid = store.upsert_finding({"key": "k", "title": "t", "category": "bug", "signature_ids": list(signature_ids),
                                "evidence": "e", "hypothesis": "h", "files": list(files), "suggested_fix": "f",
                                "impact": 1, "confidence": 0.5, "effort": 1, "trend": "new"})
    store.commit()
    return fid


def test_changes_since_lists_the_commits_that_changed_the_findings_files(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "A.scala").write_text("one\nfixed\nthree\n")
    git(repo, "commit", "-qam", "fix two")
    changes = recheck.changes_since(str(repo), "HEAD~2", "HEAD", ["A.scala:2", "not a ref"])
    assert len(changes) == 1 and changes[0].endswith("fix two")
    assert recheck.changes_since(str(repo), "HEAD", "HEAD", ["A.scala:2"]) == []


def test_look_again_reads_the_running_commit_and_starts_in_the_background(config, tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    cfg = replace(config, obp_api_source=str(repo))
    store = Store(cfg.db_path)
    fid = finding(store, signature_ids=["sig1"])
    store.db.execute("INSERT INTO deployments (git_commit, first_seen, last_seen) VALUES ('notinthecheckout', 1, 1)")
    store.commit()
    store.close()
    started = []
    monkeypatch.setattr(recheck.threading, "Thread",
                        lambda target, args, **kw: type("T", (), {"start": lambda self: started.append(args)})())
    answer = recheck.recheck(cfg, fid)
    assert answer["recheck_status"] == "running" and answer["recheck_progress"]["steps"] == 0
    assert recheck.progress(cfg.name, fid)["step"].startswith("Reading")  # shown on the page while it looks
    recheck._running.clear()
    text = started[0][2]
    assert "The instance runs HEAD now" in text  # its commit is not in the checkout
    assert "sig1: 0 times" in text and "Still an issue at" in text
    store = Store(cfg.db_path)
    assert store.db.execute("SELECT recheck_status FROM findings WHERE id = ?", (fid,)).fetchone()[0] == "running"


def test_the_answer_is_kept_with_the_finding(config, monkeypatch):
    store = Store(config.db_path)
    fid = finding(store)
    store.close()
    monkeypatch.setattr(recheck, "run_claude", lambda *a: (None, 0.1, "Fixed at abc by abc (2026-10-11, Ada): it checks.", None))
    recheck._look(config, fid, "prompt")
    store = Store(config.db_path)
    row = store.db.execute("SELECT recheck, recheck_status, rechecked_at FROM findings WHERE id = ?", (fid,)).fetchone()
    assert row["recheck"].startswith("Fixed at abc") and row["recheck_status"] == "done" and row["rechecked_at"] <= now()


def test_looking_again_at_the_security_findings_to_review_moves_the_fixed_ones_to_done(config, monkeypatch):
    store = Store(config.db_path)
    ids = {}
    for key, category, status in [("fixed", "security", "suggested"), ("still", "security", "suggested"),
                                  ("open", "security", "open"), ("bug", "bug", "suggested")]:
        ids[key] = store.upsert_finding({"key": key, "title": key, "category": category, "signature_ids": [],
                                         "evidence": "e", "hypothesis": "h", "files": [], "suggested_fix": "f",
                                         "impact": 1, "confidence": 0.5, "effort": 1, "trend": "new"})
        store.db.execute("UPDATE findings SET status = ? WHERE id = ?", (status, ids[key]))
    store.commit()
    store.close()
    looked = []
    monkeypatch.setattr(recheck, "_start", lambda cfg, fid: f"prompt {fid}")
    monkeypatch.setattr(recheck, "_look", lambda cfg, fid, text: looked.append(fid) or (
        "Fixed at abc by abc (2026-10-12, Ada): it checks." if fid == ids["fixed"] else "Still an issue at abc: no check."))
    monkeypatch.setattr(recheck.threading, "Thread",
                        lambda target, args, **kw: type("T", (), {"start": lambda self: target(*args)})())
    assert recheck.sweep(config)["total"] == 2
    assert sorted(looked) == sorted([ids["fixed"], ids["still"]])  # only security findings to review
    shown = recheck.sweep_progress(config.name)
    assert shown["running"] is False and shown["done"] == 2 and shown["fixed"] == 1
    store = Store(config.db_path)
    status = dict(store.db.execute("SELECT key, status FROM findings").fetchall())
    assert status == {"fixed": "fixed", "still": "suggested", "open": "open", "bug": "suggested"}
    comment = store.db.execute("SELECT comment FROM feedback WHERE finding_id = ?", (ids["fixed"],)).fetchone()[0]
    assert comment.startswith("Look again: Fixed at abc")
    recheck._sweeps.clear()
