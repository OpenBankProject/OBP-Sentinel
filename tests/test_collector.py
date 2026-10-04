from obp_sentinel.collector import Collector, bucket_of, new_entries
from obp_sentinel.store import Store


def test_first_poll_takes_everything():
    assert new_entries(["c", "b", "a"], []) == (["c", "b", "a"], False)


def test_only_entries_above_previous_head_are_new():
    assert new_entries(["e", "d", "c", "b", "a"], ["c", "b", "a"]) == (["e", "d"], False)


def test_nothing_new():
    assert new_entries(["c", "b", "a"], ["c", "b", "a"]) == ([], False)


def test_partial_head_at_end_of_response():
    # The cache was trimmed so only the first two of the previous head are left
    assert new_entries(["f", "e", "c", "b"], ["c", "b", "a"]) == (["f", "e"], False)


def test_head_gone_means_gap():
    assert new_entries(["z", "y"], ["c", "b", "a"]) == (["z", "y"], True)


def test_repeated_identical_messages_are_not_mistaken_for_the_head():
    # "x" alone repeats; the head is the sequence x, b
    assert new_entries(["x", "x", "b", "a"], ["x", "b", "a"]) == (["x"], False)


def test_bucket_of():
    assert bucket_of(3600 + 17 * 60, 15) == 3600 + 15 * 60


class FakeClient:
    def __init__(self, logs, telemetry):
        self.logs = logs
        self.telemetry_body = telemetry

    def log_cache(self, level, limit):
        return self.logs.get(level, [])[:limit]

    def telemetry(self):
        return self.telemetry_body


def test_poll_counts_new_signatures_and_ignores_own_calls(config, tmp_path):
    store = Store(str(tmp_path / "s.db"))
    logs = {"error": [
        "[2026-10-04 09:00:02Z] [t] [code.X] Failed for 'b2'",
        "[2026-10-04 09:00:01Z] [t] [code.X] Failed for 'b1'",
        "[2026-10-04 09:00:00Z] [t] [code.X] GET /obp/v5.1.0/system/log-cache/error failed",
    ]}
    telemetry = {"api_instance_id": "i1", "git_commit": "c1", "meters": [
        {"name": "obp.api.endpoint.requests", "type": "timer", "tags": {"operation": "getBanks", "status": "2xx"},
         "measurements": {"count": 3.0, "total_time": 0.3}},
        {"name": "jvm.threads.live", "type": "gauge", "tags": {}, "measurements": {"value": 5.0}},
    ]}
    client = FakeClient(logs, telemetry)
    collector = Collector(config, store, client)
    collector.poll_once()

    sig = store.db.execute("SELECT * FROM signatures").fetchall()
    assert len(sig) == 1 and sig[0]["total_count"] == 2
    assert store.db.execute("SELECT COUNT(*) FROM telemetry_values").fetchone()[0] == 2  # jvm.threads not kept

    # Next poll: one new entry on top
    client.logs["error"] = ["[2026-10-04 09:05:00Z] [t] [code.X] Failed for 'b3'"] + logs["error"]
    collector.poll_once()
    assert store.db.execute("SELECT total_count FROM signatures").fetchone()[0] == 3
