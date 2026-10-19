from obp_sentinel import classes
from obp_sentinel.store import Store


def finding(store, key):
    return store.upsert_finding({"key": key, "title": "t", "category": "bug", "signature_ids": [], "evidence": "e",
                                 "hypothesis": "h", "files": [], "suggested_fix": "f", "impact": 1, "confidence": 0.5,
                                 "effort": 1, "trend": "new"})


def test_classes_are_reused_counted_and_parsed(config, monkeypatch):
    store = Store(config.db_path)
    a, b, c = finding(store, "a"), finding(store, "b"), finding(store, "c")
    store.db.execute("UPDATE findings SET status = 'fixed', bug_class = 'Missing or wrong Role check' WHERE id IN (?, ?)", (a, b))
    store.db.execute("UPDATE findings SET status = 'fixed' WHERE id = ?", (c,))
    store.commit()
    assert classes.known(store)[0] == "Missing or wrong Role check"  # the most used first, then the seeds
    assert classes.known(store).count("Missing or wrong Role check") == 1 and len(classes.known(store)) == len(classes.SEEDS)
    assert classes.counts(store) == [("Missing or wrong Role check", 2)]
    store.close()
    assert classes.unclassified(config) == [c]
    assert classes.parse("Fixed: x\nClass: Not scoped to the bank in the path.\nCommit: y") == "Not scoped to the bank in the path"
    assert classes.parse("no class here") is None
    monkeypatch.setattr(classes, "ask", lambda *a: (None, "Class: Input not validated"))
    assert classes.classify(config, c) == "Input not validated"
    assert classes.unclassified(config) == []
