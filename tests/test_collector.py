from dataclasses import replace

from obp_sentinel import collector as collector_module
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

    def declare_platform_app(self):
        return {"required_scopes": []}

    def status_without_token(self, url):
        return 401

    def aggregate_metrics(self, from_ts, to_ts):
        return {"count": 0}


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


class DeclaringClient(FakeClient):
    def __init__(self):
        super().__init__({}, {"meters": []})
        self.marked = False
        self.declarations = 0

    def current_consumer_id(self):
        return "c1"

    def declare_platform_app(self):
        self.declarations += 1
        if not self.marked:
            raise Exception("OBP-35046: not a Platform App")
        return {"consumer_id": "c1", "label": "Sentinel", "required_scopes": []}


def test_declaration_is_retried_until_marked_then_not_every_poll(config, tmp_path):
    client = DeclaringClient()
    collector = Collector(config, Store(str(tmp_path / "s.db")), client)
    collector.poll_once()
    collector.poll_once()
    assert client.declarations == 2  # refused both times, still polling
    refused = collector.store.get_state("platform_app")
    assert not refused["ok"] and refused["consumer_id"] == "c1"  # so the operator is told the consumer_id to mark

    client.marked = True
    collector.poll_once()
    collector.poll_once()
    assert client.declarations == 3  # accepted once, not repeated on the next poll


class MetricsClient(FakeClient):
    def __init__(self):
        super().__init__({}, {"meters": []})
        self.windows = []

    def aggregate_metrics(self, from_ts, to_ts):
        self.windows.append((from_ts, to_ts))
        return {"count": 10, "average_response_time": 20.0, "maximum_response_time": 90.0, "distinct_consumer_count": 2}


def test_metrics_fetched_once_per_ended_bucket(config, tmp_path, monkeypatch):
    clock = [1791104400 + 20 * 60]  # 09:20: the 09:00 bucket ended 5 minutes ago
    monkeypatch.setattr(collector_module, "now", lambda: clock[0])
    client = MetricsClient()
    store = Store(str(tmp_path / "s.db"))
    collector = Collector(config, store, client)

    collector.poll_metrics()
    assert client.windows == [(1791104400, 1791104400 + 900)]  # no backfill on the first run
    collector.poll_metrics()
    assert len(client.windows) == 1  # the 09:15 bucket has not ended yet

    clock[0] += 60 * 60  # an hour later: catch up the four buckets that ended since
    collector.poll_metrics()
    assert [w[0] for w in client.windows[1:]] == [1791104400 + 900 * i for i in range(1, 5)]
    row = store.db.execute("SELECT * FROM metric_buckets WHERE bucket_start = 1791104400").fetchone()
    assert row["count"] == 10 and row["avg_ms"] == 20.0 and row["distinct_consumers"] == 2


def test_root_endpoint_records_which_commit_runs(config, tmp_path):
    from obp_sentinel.summary import coverage

    store = Store(str(tmp_path / "s.db"))
    client = FakeClient({}, {"meters": []})
    c = Collector(config, store, client)
    client.root = lambda: {"git_commit": "aaa1111"}
    c.poll_root()
    c.poll_root()
    client.root = lambda: {"git_commit": "bbb2222"}
    c.poll_root()
    deployments = coverage(store, config, 0, 2**40)["deployments"]
    assert [d["git_commit"] for d in deployments] == ["aaa1111", "bbb2222"]
    store.close()


def test_declaration_and_root_outcomes_are_kept_for_the_page(config, tmp_path):
    store = Store(str(tmp_path / "s.db"))
    client = DeclaringClient()
    client.root = lambda: {"git_commit": "aaa111"}
    collector = Collector(config, store, client)
    collector.poll_once()
    assert store.get_state("platform_app")["ok"] is False and "OBP-35046" in store.get_state("platform_app")["error"]
    assert store.get_state("root")["ok"] is True
    client.marked = True
    collector.poll_once()
    assert store.get_state("platform_app") == {**store.get_state("platform_app"), "ok": True, "consumer_id": "c1", "missing": []}


def test_telemetry_is_pruned_sooner_than_aggregates(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    t = collector_module.now()
    store.db.execute("INSERT INTO signatures (id, level, logger, exception, endpoint, template, first_seen, last_seen)"
                     " VALUES ('sig', 'ERROR', 'c.Foo', '', '', 'problem', ?, ?)", (t - 10 * 86400, t))
    for age_hours in (72, 47, 1):
        ts = t - age_hours * 3600
        store.record_telemetry(ts, "i1", "abc", [("obp.api.endpoint.requests", "counter", "{}", "count", 1.0)] * 3)
        store.db.execute("INSERT INTO observations (signature_id, bucket_start, count) VALUES ('sig', ?, 1)", (ts,))
        store.record_metric_bucket(ts, {"count": 5})
    store.record_metric_bucket(t - 15 * 86400, {"count": 5})  # past the 14 days

    store.prune(retention_days=14, telemetry_retention_hours=48)

    snapshots = [r[0] for r in store.db.execute("SELECT ts FROM telemetry_snapshots ORDER BY ts")]
    assert snapshots == [t - 47 * 3600, t - 3600]
    # The values of the pruned snapshot went with it
    assert store.db.execute("SELECT COUNT(*) FROM telemetry_values").fetchone()[0] == 6
    assert store.db.execute(
        "SELECT COUNT(*) FROM telemetry_values WHERE snapshot_id NOT IN (SELECT id FROM telemetry_snapshots)"
    ).fetchone()[0] == 0
    # Aggregates keep the 72h-old row until retention_days
    assert store.db.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 3
    assert [r[0] for r in store.db.execute("SELECT bucket_start FROM metric_buckets ORDER BY bucket_start")] == [
        t - 72 * 3600, t - 47 * 3600, t - 3600]


def test_poll_prunes_with_the_telemetry_retention(config, tmp_path, monkeypatch):
    calls = []
    store = Store(str(tmp_path / "s.db"))
    monkeypatch.setattr(store, "prune", lambda *args: calls.append(args))
    collector = Collector(replace(config, retention_days=9, telemetry_retention_hours=12), store, None)
    # No OBP-API in tests: every part of a poll answers at once
    collector.declare_platform_app = lambda: None
    collector.poll_logs = lambda level: f"{level} ok"
    for part in ("poll_root", "poll_telemetry", "poll_metrics", "poll_reach", "check_secured"):
        setattr(collector, part, lambda: "ok")
    collector.poll_once()
    assert calls == [(9, 12)]
