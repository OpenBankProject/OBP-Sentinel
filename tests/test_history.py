import json
import subprocess
from dataclasses import replace

from obp_sentinel import history
from obp_sentinel.store import Store


def git(repo, *args, author="Ada <ada@example.com>"):
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=x", "-c", "user.email=x@x", *args,
                    *(["--author", author] if args[0] == "commit" else [])], check=True, capture_output=True)


def make_repo(tmp_path):
    repo = tmp_path / "src"
    repo.mkdir()
    git(repo, "init", "-q")
    f = repo / "A.scala"
    f.write_text("one\ntwo\nthree\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "first", author="Ada <ada@example.com>")
    f.write_text("one\nTWO\nthree\n")
    git(repo, "commit", "-qam", "change two", author="Bob <bob@example.com>")
    (repo / "B.scala").write_text("other\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "unrelated", author="Cy <cy@example.com>")
    return repo


def test_line_history_lists_the_commits_that_changed_the_lines_oldest_first(tmp_path):
    repo = make_repo(tmp_path)
    h = history.line_history(str(repo), "A.scala:2")
    assert [(c["subject"], c["author"]) for c in h["commits"]] == [("first", "Ada"), ("change two", "Bob")]
    assert history.line_history(str(repo), "A.scala")["error"]


def test_trace_records_the_history_at_once_and_starts_judging(config, tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    cfg = replace(config, obp_api_source=str(repo))
    store = Store(cfg.db_path)
    fid = store.upsert_finding({"key": "k", "title": "t", "category": "bug", "signature_ids": [], "evidence": "e",
                                "hypothesis": "h", "files": ["A.scala:2"], "suggested_fix": "f", "impact": 1,
                                "confidence": 0.5, "effort": 1, "trend": "new"})
    store.commit()
    store.close()
    judged = []
    monkeypatch.setattr(history.threading, "Thread",
                        lambda target, args, **kw: type("T", (), {"start": lambda self: judged.append(args)})())
    out = history.trace(cfg, fid)
    assert out["trace_status"] == "running" and len(out["history"][0]["commits"]) == 2
    assert judged and "change two" in history.prompt(judged[0][2], judged[0][3], str(repo))
    store = Store(cfg.db_path)
    row = store.db.execute("SELECT history, trace_status FROM findings WHERE id = ?", (fid,)).fetchone()
    assert json.loads(row["history"])[0]["file"] == "A.scala:2" and row["trace_status"] == "running"


def test_who_when_commit_and_where_are_split_out_of_the_answer():
    from obp_sentinel.store import introduced_parts
    assert introduced_parts("Introduced in d7bd34844 (2025-04-24, Ada) in obp-api/src/A.scala: it removed the role.") \
        == ("d7bd34844", "2025-04-24", "Ada", "obp-api/src/A.scala")
    assert introduced_parts("Introduced in bab4919ff (2018-10-22, Grace Hopper): Added endpoint.") \
        == ("bab4919ff", "2018-10-22", "Grace Hopper", None)
    assert introduced_parts("Predates abc1234 (2019-01-01): it was there.") == (None, None, None, None)



def test_trace_history_runs_by_itself_for_the_findings_never_traced(config, monkeypatch):
    import threading
    from obp_sentinel import history
    from obp_sentinel.store import Store
    store = Store(config.db_path)
    ids = [store.upsert_finding({"key": k, "title": "t", "category": "bug", "signature_ids": [], "evidence": "e",
                                 "hypothesis": "h", "files": [], "suggested_fix": "f", "impact": i, "confidence": 0.5,
                                 "effort": 1, "trend": "new"}) for k, i in (("low", 1), ("high", 5), ("done", 3))]
    store.db.execute("UPDATE findings SET trace_status = 'done' WHERE id = ?", (ids[2],))
    store.commit()
    store.close()
    assert history.untraced(config) == [ids[1], ids[0]]  # most important first, the traced one left out
    stopped, traced = threading.Event(), []
    monkeypatch.setattr(history, "unavailable_reason", lambda c, scheduled: None)
    monkeypatch.setattr(history, "trace", lambda c, fid, background: traced.append((fid, background)) or (fid == ids[0] and stopped.set()))
    monkeypatch.setattr(stopped, "wait", lambda seconds: stopped.is_set())
    history.trace_forever([config], stopped)
    assert traced == [(ids[1], False), (ids[0], False)]  # one at a time, here and now


def test_the_commit_the_evidence_was_read_at_may_be_short(tmp_path):
    from obp_sentinel.history import read_at
    repo = make_repo(tmp_path)
    full = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD~1"], capture_output=True, text=True).stdout.strip()
    assert read_at(str(repo), f"Whilst reading the source code at {full[:9]} I noticed 1234567 calls") == full[:9]
    assert read_at(str(repo), f"Whilst reading the source code at {full} I noticed") == full
    assert read_at(str(repo), "no commit here, 1234567 times") == "HEAD"
