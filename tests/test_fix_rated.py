import pytest

from obp_sentinel import fixer
from obp_sentinel.store import Store


def finding(key, impact=3):
    return {
        "key": key, "title": f"Problem {key}", "category": "bug", "signature_ids": [], "evidence": "e",
        "hypothesis": "h", "files": [], "suggested_fix": "f", "impact": impact, "confidence": 0.8, "effort": 1,
        "trend": "stable",
    }


@pytest.fixture
def rated(config, monkeypatch):
    """a: Relevant, b: Important (lower priority), c: Urgent then Dismiss, d: Important then Skip for now,
    e: not rated, f: Relevant but fixed, g: Urgent but Fix this already failed."""
    monkeypatch.setattr(fixer, "start", lambda config: None)
    store = Store(config.db_path)
    ids = {}
    for key, impact in (("a", 5), ("b", 1), ("c", 3), ("d", 3), ("e", 3), ("f", 3), ("g", 3)):
        ids[key] = store.upsert_finding(finding(key, impact))
    for key, verdicts in (("a", ["relevant"]), ("b", ["important"]), ("c", ["urgent", "dismissed"]),
                          ("d", ["important", "later"]), ("f", ["relevant", "fixed"]), ("g", ["urgent"])):
        for verdict in verdicts:
            store.add_feedback(ids[key], verdict, None, 7)
    store.db.execute("UPDATE findings SET fix_status = 'failed' WHERE id = ?", (ids["g"],))
    store.commit()
    store.close()
    return ids


def test_only_rated_findings_never_tried_are_fixed_most_urgent_first(config, rated):
    store = Store(config.db_path)
    assert fixer.rated_to_fix(store) == [rated["b"], rated["a"]]  # Important before Relevant, whatever the priority
    assert fixer.rated_to_fix(store, "important") == [rated["b"]]
    assert fixer.rated_to_fix(store, "urgent") == []  # c was dismissed, g already tried
    store.close()


def test_fix_all_of_one_rating_queues_only_those_with_tests(config, rated):
    assert fixer.queue_rated(config, "relevant") == {"queued": 1}
    store = Store(config.db_path)
    queued = store.db.execute("SELECT id, fix_tests FROM findings WHERE fix_status = 'queued'").fetchall()
    store.close()
    assert [tuple(r) for r in queued] == [(rated["a"], 1)]
    assert fixer.queue_rated(config, "important") == {"queued": 1}
    assert fixer._next(config) == rated["b"]  # Important before Relevant
    assert fixer.queue_rated(config, "relevant") == {"queued": 0}  # already queued


def test_fix_all_without_tests_queues_them_without_tests(config, rated):
    assert fixer.queue_rated(config, "important", tests=False) == {"queued": 1}
    store = Store(config.db_path)
    queued = store.db.execute("SELECT id, fix_tests FROM findings WHERE fix_status = 'queued'").fetchall()
    store.close()
    assert [tuple(r) for r in queued] == [(rated["b"], 0)]
