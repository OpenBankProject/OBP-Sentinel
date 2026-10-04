import pytest

from obp_sentinel import digest as digest_module
from obp_sentinel.digest import NotReady, write_digest
from obp_sentinel.signatures import parse_line, signature_of
from obp_sentinel.store import Store, priority_of
from obp_sentinel.summary import counter_delta

BASE = 1_790_000_000


def finding(key, impact=4, confidence=0.8, effort=1, trend="rising", signature_ids=()):
    return {
        "key": key, "title": f"Problem {key}", "category": "bug", "signature_ids": list(signature_ids),
        "evidence": "e", "hypothesis": "h", "files": ["a.scala:1"], "suggested_fix": "f",
        "impact": impact, "confidence": confidence, "effort": effort, "trend": trend,
    }


@pytest.fixture
def store(config):
    s = Store(config.db_path)
    yield s
    s.close()


def watch(store, hours, start=BASE):
    for i in range(int(hours * 30) + 1):  # a poll every 2 minutes
        store.db.execute("INSERT INTO polls (ts, source, ok) VALUES (?, 'log:error', 1)", (start + i * 120,))


@pytest.fixture
def at(monkeypatch):
    def set_now(ts):
        monkeypatch.setattr(digest_module, "now", lambda: ts)
    return set_now


def test_priority():
    assert priority_of(4, 0.5, 2, "stable") == 1.0
    assert priority_of(4, 0.5, 2, "new") == 1.5


def test_refuses_before_enough_watching(store, config, tmp_path, at):
    watch(store, 2)
    at(BASE + 2 * 3600)
    with pytest.raises(NotReady):
        write_digest(store, config, str(tmp_path / "d"))


def test_top_findings_only_and_never_repeated(store, config, tmp_path, at):
    for key in "abcde":
        store.upsert_finding(finding(key))
    store.upsert_finding(finding("weak", impact=1, confidence=0.3))  # priority below the threshold
    watch(store, 5)
    at(BASE + 5 * 3600)
    path = write_digest(store, config, str(tmp_path / "d"))
    text = open(path).read()
    assert text.count("## ") == 3
    assert "weak" not in text

    watch(store, 5, start=BASE + 5 * 3600)
    at(BASE + 10 * 3600)
    second = open(write_digest(store, config, str(tmp_path / "d2"))).read()
    assert second.count("## ") == 2  # only the two not yet suggested
    for key in "abc":
        assert f"`{key}`" not in second


def test_weekly_limit(store, config, tmp_path, at):
    for key in "abcdefghijkl":
        store.upsert_finding(finding(key))
    t = BASE
    shown = 0
    for _ in range(5):
        watch(store, 5, start=t)
        t += 5 * 3600
        at(t)
        shown += open(write_digest(store, config, str(tmp_path / f"d{t}"))).read().count("## ")
    assert shown == config.max_per_week


def test_one_off_signature_is_not_suggested(store, config, tmp_path, at):
    sig = signature_of("error", parse_line("[2026-10-04 09:00:00Z] [t] [code.X] Boom"))
    store.record_occurrence(sig, BASE, BASE, "raw")
    store.upsert_finding(finding("once", signature_ids=[sig.id]))
    watch(store, 5)
    at(BASE + 5 * 3600)
    text = open(write_digest(store, config, str(tmp_path / "d"))).read()
    assert "Nothing worth your attention" in text

    store.record_occurrence(sig, BASE + 3600, BASE + 3600, "raw")  # seen again, in another bucket
    watch(store, 5, start=BASE + 5 * 3600)
    at(BASE + 10 * 3600)
    assert "`once`" in open(write_digest(store, config, str(tmp_path / "d2"))).read()


def test_dismissed_hides_finding_and_signatures(store, config):
    sig = signature_of("error", parse_line("[2026-10-04 09:00:00Z] [t] [code.X] Boom"))
    store.record_occurrence(sig, BASE, BASE, "raw")
    fid = store.upsert_finding(finding("x", signature_ids=[sig.id]))
    store.add_feedback(fid, "dismissed", "expected", config.snooze_days)
    store.upsert_finding(finding("x", impact=5))  # analyst reports it again
    row = store.db.execute("SELECT status FROM findings WHERE id = ?", (fid,)).fetchone()
    assert row["status"] == "dismissed"
    assert store.db.execute("SELECT status FROM signatures").fetchone()["status"] == "dismissed"


def test_counter_delta_handles_restart():
    points = [(10, "a", 100.0), (20, "a", 150.0), (30, "b", 20.0), (40, "b", 50.0)]
    assert counter_delta(points, 10, 40) == 50 + 20 + 30
    assert counter_delta(points, 25, 40) == 20 + 30
