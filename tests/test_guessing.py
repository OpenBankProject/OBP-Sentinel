from obp_sentinel import fixer, guessing
from obp_sentinel.store import NEEDS_ID_WEIGHT, Store


def finding(store, key, impact=5):
    return store.upsert_finding({"key": key, "title": "t", "category": "security", "signature_ids": [], "evidence": "e",
                                 "hypothesis": "h", "files": [], "suggested_fix": "f", "impact": impact,
                                 "confidence": 1, "effort": 1, "trend": "stable"})


def test_a_problem_that_needs_a_guessed_id_comes_later(config, monkeypatch):
    store = Store(config.db_path)
    guessed, open_ = finding(store, "guessed"), finding(store, "open", impact=2)
    store.commit()
    store.close()
    assert guessing.unjudged(config) == [guessed, open_]
    monkeypatch.setattr(guessing, "ask", lambda c, text, budget: (None, "Needs a guessed id: yes, a UUID nobody lists"))
    assert guessing.judge(config, guessed) is True
    monkeypatch.setattr(guessing, "ask", lambda c, text, budget: (None, "Needs a guessed id: no, open to all"))
    assert guessing.judge(config, open_) is False
    store = Store(config.db_path)
    priority = dict(store.db.execute("SELECT id, priority FROM findings"))
    assert priority[guessed] == 5 * NEEDS_ID_WEIGHT and priority[open_] == 2  # the guessed one now comes after
    finding(store, "guessed")  # the analyst records it again, not saying: the judgement stays
    assert store.db.execute("SELECT needs_id FROM findings WHERE id = ?", (guessed,)).fetchone()[0] == 1
    store.db.execute("UPDATE findings SET fix_status = 'queued', fix_at = 1")
    store.commit()
    store.close()
    assert fixer._next(config) == open_  # Fix this takes the most important first
