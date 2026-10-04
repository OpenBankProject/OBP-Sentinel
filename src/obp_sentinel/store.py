"""Sentinel's memory: a SQLite database. All times are Unix epoch seconds (UTC)."""

import json
import sqlite3
import time

SCHEMA = """
-- One row per poll of one source, so we know how long and how well we have been watching.
CREATE TABLE IF NOT EXISTS polls (
    id         INTEGER PRIMARY KEY,
    ts         INTEGER NOT NULL,
    source     TEXT NOT NULL,              -- 'log:error', 'log:warning', ..., 'telemetry'
    ok         INTEGER NOT NULL,           -- 0 when the poll failed
    fetched    INTEGER NOT NULL DEFAULT 0,
    new        INTEGER NOT NULL DEFAULT 0,
    gap        INTEGER NOT NULL DEFAULT 0, -- 1 when entries may have been missed since the last poll
    error      TEXT
);
CREATE INDEX IF NOT EXISTS polls_ts ON polls(ts);

-- One row per distinct problem.
CREATE TABLE IF NOT EXISTS signatures (
    id          TEXT PRIMARY KEY,
    level       TEXT NOT NULL,
    logger      TEXT NOT NULL,
    exception   TEXT NOT NULL,
    endpoint    TEXT NOT NULL,
    template    TEXT NOT NULL,
    first_seen  INTEGER NOT NULL,
    last_seen   INTEGER NOT NULL,
    total_count INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'active'  -- active | dismissed (never suggest again)
);

-- How often each signature occurred, per time bucket.
CREATE TABLE IF NOT EXISTS observations (
    signature_id TEXT NOT NULL REFERENCES signatures(id),
    bucket_start INTEGER NOT NULL,
    count        INTEGER NOT NULL,
    PRIMARY KEY (signature_id, bucket_start)
);
CREATE INDEX IF NOT EXISTS observations_bucket ON observations(bucket_start);

-- A few raw examples per signature, for the analyst to read.
CREATE TABLE IF NOT EXISTS samples (
    id           INTEGER PRIMARY KEY,
    signature_id TEXT NOT NULL REFERENCES signatures(id),
    ts           INTEGER NOT NULL,
    message      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS samples_signature ON samples(signature_id);

CREATE TABLE IF NOT EXISTS telemetry_snapshots (
    id              INTEGER PRIMARY KEY,
    ts              INTEGER NOT NULL,
    api_instance_id TEXT,
    git_commit      TEXT
);
CREATE INDEX IF NOT EXISTS telemetry_snapshots_ts ON telemetry_snapshots(ts);

CREATE TABLE IF NOT EXISTS telemetry_values (
    snapshot_id INTEGER NOT NULL REFERENCES telemetry_snapshots(id) ON DELETE CASCADE,
    meter       TEXT NOT NULL,
    type        TEXT NOT NULL,
    tags        TEXT NOT NULL,             -- JSON object with sorted keys
    stat        TEXT NOT NULL,             -- count | total_time | max | value | ...
    value       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS telemetry_values_snapshot ON telemetry_values(snapshot_id);

-- The analyst's conclusions. `key` is a stable slug the analyst reuses for the same problem.
CREATE TABLE IF NOT EXISTS findings (
    id            INTEGER PRIMARY KEY,
    key           TEXT NOT NULL UNIQUE,
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL,
    title         TEXT NOT NULL,
    category      TEXT NOT NULL,           -- bug | performance | security | reliability | readability | api-contract
    signature_ids TEXT NOT NULL,           -- JSON list
    evidence      TEXT NOT NULL,
    hypothesis    TEXT NOT NULL,
    files         TEXT NOT NULL,           -- JSON list of "path:line"
    suggested_fix TEXT NOT NULL,
    impact        INTEGER NOT NULL,        -- 1..5
    confidence    REAL NOT NULL,           -- 0..1
    effort        INTEGER NOT NULL,        -- 1..5
    trend         TEXT NOT NULL,           -- new | rising | stable | falling
    priority      REAL NOT NULL,
    status        TEXT NOT NULL DEFAULT 'open',  -- open | suggested | accepted | dismissed | later | fixed
    snoozed_until INTEGER
);

-- What was shown to people, and when.
CREATE TABLE IF NOT EXISTS suggestions (
    id         INTEGER PRIMARY KEY,
    finding_id INTEGER NOT NULL REFERENCES findings(id),
    ts         INTEGER NOT NULL,
    digest     TEXT NOT NULL
);

-- What people said about it.
CREATE TABLE IF NOT EXISTS feedback (
    id         INTEGER PRIMARY KEY,
    finding_id INTEGER NOT NULL REFERENCES findings(id),
    ts         INTEGER NOT NULL,
    verdict    TEXT NOT NULL,              -- accepted | dismissed | later | fixed
    comment    TEXT
);

CREATE TABLE IF NOT EXISTS state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

SAMPLES_PER_SIGNATURE = 5
TREND_WEIGHTS = {"new": 1.5, "rising": 1.3, "stable": 1.0, "falling": 0.6}
CATEGORIES = {"bug", "performance", "security", "reliability", "readability", "api-contract"}


def now() -> int:
    return int(time.time())


def priority_of(impact: int, confidence: float, effort: int, trend: str) -> float:
    return round(impact * confidence * TREND_WEIGHTS[trend] / effort, 3)


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    # --- state -------------------------------------------------------------------

    def get_state(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_state(self, key: str, value) -> None:
        self.db.execute(
            "INSERT INTO state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )

    # --- collecting ------------------------------------------------------------------

    def record_poll(self, source: str, ok: bool, fetched=0, new=0, gap=False, error: str | None = None) -> None:
        self.db.execute(
            "INSERT INTO polls (ts, source, ok, fetched, new, gap, error) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (now(), source, int(ok), fetched, new, int(gap), error),
        )

    def record_occurrence(self, sig, ts: int, bucket_start: int, raw: str) -> None:
        self.db.execute(
            """INSERT INTO signatures (id, level, logger, exception, endpoint, template, first_seen, last_seen, total_count)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
               ON CONFLICT(id) DO UPDATE SET
                 total_count = total_count + 1,
                 first_seen = MIN(first_seen, excluded.first_seen),
                 last_seen = MAX(last_seen, excluded.last_seen),
                 endpoint = CASE WHEN endpoint = '' THEN excluded.endpoint ELSE endpoint END""",
            (sig.id, sig.level, sig.logger, sig.exception, sig.endpoint, sig.template, ts, ts),
        )
        self.db.execute(
            """INSERT INTO observations (signature_id, bucket_start, count) VALUES (?, ?, 1)
               ON CONFLICT(signature_id, bucket_start) DO UPDATE SET count = count + 1""",
            (sig.id, bucket_start),
        )
        kept = self.db.execute("SELECT COUNT(*) FROM samples WHERE signature_id = ?", (sig.id,)).fetchone()[0]
        if kept < SAMPLES_PER_SIGNATURE:
            self.db.execute(
                "INSERT INTO samples (signature_id, ts, message) VALUES (?, ?, ?)", (sig.id, ts, raw[:4000])
            )

    def record_telemetry(self, ts: int, api_instance_id: str | None, git_commit: str | None, values: list[tuple]) -> None:
        cur = self.db.execute(
            "INSERT INTO telemetry_snapshots (ts, api_instance_id, git_commit) VALUES (?, ?, ?)",
            (ts, api_instance_id, git_commit),
        )
        self.db.executemany(
            "INSERT INTO telemetry_values (snapshot_id, meter, type, tags, stat, value) VALUES (?, ?, ?, ?, ?, ?)",
            [(cur.lastrowid, *v) for v in values],
        )

    def prune(self, retention_days: int) -> None:
        cutoff = now() - retention_days * 86400
        self.db.execute("DELETE FROM telemetry_snapshots WHERE ts < ?", (cutoff,))
        self.db.execute("DELETE FROM observations WHERE bucket_start < ?", (cutoff,))
        self.db.execute("DELETE FROM polls WHERE ts < ?", (cutoff,))

    def commit(self) -> None:
        self.db.commit()

    # --- findings, suggestions, feedback --------------------------------------------

    def upsert_finding(self, f: dict) -> int:
        """Insert or update a finding by its key. Dismissed and fixed findings keep their status."""
        trend = f["trend"]
        if trend not in TREND_WEIGHTS:
            raise ValueError(f"trend must be one of {sorted(TREND_WEIGHTS)}, not {trend!r}")
        if f["category"] not in CATEGORIES:
            raise ValueError(f"category must be one of {sorted(CATEGORIES)}, not {f['category']!r}")
        impact, effort, confidence = int(f["impact"]), int(f["effort"]), float(f["confidence"])
        if not (1 <= impact <= 5 and 1 <= effort <= 5 and 0 <= confidence <= 1):
            raise ValueError("impact and effort must be 1..5, confidence 0..1")
        ts = now()
        fields = (
            f["title"], f["category"], json.dumps(f.get("signature_ids", [])), f["evidence"], f["hypothesis"],
            json.dumps(f.get("files", [])), f["suggested_fix"], impact, confidence, effort, trend,
            priority_of(impact, confidence, effort, trend),
        )
        existing = self.db.execute("SELECT id FROM findings WHERE key = ?", (f["key"],)).fetchone()
        if existing:
            self.db.execute(
                """UPDATE findings SET updated_at = ?, title = ?, category = ?, signature_ids = ?, evidence = ?,
                   hypothesis = ?, files = ?, suggested_fix = ?, impact = ?, confidence = ?, effort = ?, trend = ?,
                   priority = ? WHERE id = ?""",
                (ts, *fields, existing["id"]),
            )
            return existing["id"]
        cur = self.db.execute(
            """INSERT INTO findings (key, created_at, updated_at, title, category, signature_ids, evidence, hypothesis,
               files, suggested_fix, impact, confidence, effort, trend, priority)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (f["key"], ts, ts, *fields),
        )
        return cur.lastrowid

    def findings(self, statuses: tuple[str, ...] | None = None) -> list[sqlite3.Row]:
        if statuses:
            marks = ",".join("?" * len(statuses))
            return self.db.execute(
                f"SELECT * FROM findings WHERE status IN ({marks}) ORDER BY priority DESC", statuses
            ).fetchall()
        return self.db.execute("SELECT * FROM findings ORDER BY priority DESC").fetchall()

    def add_feedback(self, finding_id: int, verdict: str, comment: str | None, snooze_days: int) -> None:
        finding = self.db.execute("SELECT * FROM findings WHERE id = ?", (finding_id,)).fetchone()
        if not finding:
            raise ValueError(f"No finding with id {finding_id}")
        ts = now()
        self.db.execute(
            "INSERT INTO feedback (finding_id, ts, verdict, comment) VALUES (?, ?, ?, ?)",
            (finding_id, ts, verdict, comment),
        )
        snoozed_until = ts + snooze_days * 86400 if verdict == "later" else None
        self.db.execute(
            "UPDATE findings SET status = ?, snoozed_until = ? WHERE id = ?", (verdict, snoozed_until, finding_id)
        )
        if verdict == "dismissed":
            for signature_id in json.loads(finding["signature_ids"]):
                self.set_signature_status(signature_id, "dismissed")

    def set_signature_status(self, signature_id: str, status: str) -> bool:
        cur = self.db.execute("UPDATE signatures SET status = ? WHERE id = ?", (status, signature_id))
        return cur.rowcount > 0

    def suggestions_since(self, ts: int) -> int:
        return self.db.execute("SELECT COUNT(*) FROM suggestions WHERE ts >= ?", (ts,)).fetchone()[0]

    def record_suggestion(self, finding_id: int, digest: str, ts: int) -> None:
        self.db.execute(
            "INSERT INTO suggestions (finding_id, ts, digest) VALUES (?, ?, ?)", (finding_id, ts, digest)
        )
        self.db.execute("UPDATE findings SET status = 'suggested' WHERE id = ?", (finding_id,))
