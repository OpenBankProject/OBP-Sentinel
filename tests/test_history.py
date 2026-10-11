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
