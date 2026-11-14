from dataclasses import replace

import pytest

from obp_sentinel import preferences
from obp_sentinel.digest import write_digest
from obp_sentinel.preferences import forget, learn, rank, weight_of
from obp_sentinel.store import Store
from obp_sentinel.summary import build_summary, to_markdown


def finding(key, category="bug", impact=3, confidence=0.8, effort=1):
    return {
        "key": key, "title": f"Problem {key}", "category": category, "signature_ids": [],
        "evidence": "e", "hypothesis": "h", "files": [], "suggested_fix": "f",
        "impact": impact, "confidence": confidence, "effort": effort, "trend": "stable",
    }


@pytest.fixture(autouse=True)
def no_cache():
    preferences.clear()
    yield
    preferences.clear()


@pytest.fixture
def two(config, tmp_path):
    """Two instances, each with its own database."""
    return [config, replace(config, name="other", db_path=str(tmp_path / "other.db"))]


def respond(config, responses):
    """`responses`: (key, category, verdict, comment) for findings to add and respond to."""
    store = Store(config.db_path)
    try:
        for key, category, verdict, comment in responses:
            finding_id = store.upsert_finding(finding(key, category))
            store.add_feedback(finding_id, verdict, comment, 7)
        store.commit()
    finally:
        store.close()


def test_weight_needs_a_few_responses_and_stays_between_half_and_one_and_a_half():
    assert weight_of(2, 0) == 1.0
    assert weight_of(3, 0) == 1.3
    assert weight_of(0, 3) == 0.7
    assert weight_of(0, 100) == 0.51
    assert weight_of(100, 0) == 1.49


def test_learns_across_instances_from_the_latest_response_on_each_finding(two):
    respond(two[0], [("s1", "security", "relevant", None), ("s2", "security", "fixed", None),
                     ("r1", "readability", "dismissed", "Style only")])
    respond(two[1], [("s3", "security", "acted", None), ("r2", "readability", "dismissed", None),
                     ("r3", "readability", "later", None)])
    store = Store(two[1].db_path)
    store.add_feedback(store.upsert_finding(finding("r2", "readability")), "relevant", None, 7)  # changed their mind
    store.commit()
    store.close()
    learned = learn(two)
    assert learned["categories"] == {
        "security": {"acted": 3, "urgent": 0, "dismissed": 0, "weight": 1.3},
        "readability": {"acted": 1, "urgent": 0, "dismissed": 1, "weight": 1.0},
    }
    assert learned["dismissal_comments"] == [
        {"instance": two[0].name, "category": "readability", "title": "Problem r1", "comment": "Style only"}]


def test_look_again_marking_fixed_does_not_count(config):
    store = Store(config.db_path)
    finding_id = store.upsert_finding(finding("s1", "security"))
    store.add_feedback(finding_id, "fixed", "Look again: Fixed at abc", 7, from_sentinel=True)
    store.commit()
    store.close()
    assert learn([config])["categories"] == {}


def test_high_impact_is_never_ranked_lower():
    learned = {"categories": {"readability": {"acted": 0, "dismissed": 5, "weight": 0.64}}}
    low = {"category": "readability", "impact": 3, "priority": 2.0}
    high = {"category": "readability", "impact": 4, "priority": 2.0}
    assert rank(low, learned) == 1.28
    assert rank(high, learned) == 2.0
    assert rank({"category": "bug", "impact": 1, "priority": 2.0}, learned) == 2.0


def test_forget_ignores_the_responses_so_far_on_every_instance(two, monkeypatch):
    respond(two[0], [(f"s{i}", "security", "relevant", None) for i in range(3)])
    respond(two[1], [(f"r{i}", "readability", "dismissed", "No") for i in range(3)])
    assert learn(two)["responses"] == 6
    monkeypatch.setattr(preferences, "now", lambda: 4_000_000_000)
    forget(two)
    assert learn(two) == {"categories": {}, "dismissal_comments": [], "responses": 0}


def test_the_digest_puts_what_the_person_acts_on_first(config, tmp_path):
    store = Store(config.db_path)
    store.upsert_finding(finding("bug-a", "bug", impact=3))  # priority 2.4
    store.upsert_finding(finding("sec-a", "security", impact=3, confidence=0.7))  # priority 2.1
    store.commit()
    learned = {"categories": {"security": {"acted": 5, "dismissed": 0, "weight": 1.36}}}
    path = write_digest(store, replace(config, digest_size=1), str(tmp_path / "d"), force=True, learned=learned)
    store.close()
    assert "Problem sec-a" in open(path).read()


def test_the_summary_tells_the_analyst_what_the_person_acts_on(config):
    respond(config, [("r1", "readability", "dismissed", "Expected when clients send bad IBANs")])
    store = Store(config.db_path)
    text = to_markdown(build_summary(store, config, 6, learned=learn([config])))
    store.close()
    assert "- readability: acted on 0, dismissed 1" in text
    assert "Problem r1: Expected when clients send bad IBANs" in text


def test_important_counts_twice_and_urgent_three_times(config):
    respond(config, [("a", "bug", "relevant", None), ("b", "bug", "dismissed", None), ("c", "bug", "dismissed", None)])
    assert learn([config])["categories"]["bug"]["weight"] == 0.9  # one Relevant, two Dismiss
    preferences.clear()
    respond(config, [("d", "performance", "urgent", None), ("e", "performance", "dismissed", None),
                     ("f", "performance", "dismissed", None)])
    assert learn([config])["categories"]["performance"] == {"acted": 1, "urgent": 1, "dismissed": 2,
                                                             "weight": weight_of(1, 2, 3)}
    assert weight_of(1, 2, 3) == 1.07  # one Urgent outweighs two Dismiss


def test_a_rating_marks_the_finding_accepted(config):
    store = Store(config.db_path)
    finding_id = store.upsert_finding(finding("a"))
    store.add_feedback(finding_id, "important", None, 7)
    assert store.findings()[0]["status"] == "accepted"
    assert store.db.execute("SELECT verdict FROM feedback").fetchone()[0] == "important"
    store.close()
