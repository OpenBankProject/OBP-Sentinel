"""What Sentinel learns from the responses a person gives: which kinds of finding they act on and which they dismiss.

Only deliberate responses count: the buttons on the page, `sentinel feedback` and committing a fix, with their
comments. Look again marking a finding fixed does not count, because Sentinel decided that, not the person; nor does
anything about what a person opens or reads.
Responses are pooled across all instances, because the same person answers them all.

The learning changes only the order in which findings are shown, never their priority or whether they are kept:
- Each category gets a weight between 0.5 and 1.5, from the share of its findings the person acted on, an Important
  counting twice and an Urgent three times. A category with fewer than `MIN_RESPONSES` responses keeps a weight of 1.
- A finding with impact of `HIGH_IMPACT` or more is never ranked lower, so Sentinel cannot learn to stay quiet
  about something serious.
- Forget (on the page) ignores the responses given before it, on every instance.
"""

import os
import sqlite3
import threading
import time

from .config import Config
from .store import CATEGORIES, Store, now

# The person found the finding worth something, and how much: Urgent counts three times, Important twice
WEIGHTS = {"relevant": 1, "important": 2, "urgent": 3, "accepted": 1, "acted": 1, "fixed": 1}
ACTED_ON = tuple(WEIGHTS)
DISMISSED = ("dismissed",)
MIN_RESPONSES = 3  # fewer than this in a category and its weight stays 1
HIGH_IMPACT = 4  # impact from which a finding is never ranked lower
MAX_COMMENTS = 10  # dismissal comments passed to the analyst
LEARN_SINCE = "learn_since"  # state key: responses before this are ignored (set by Forget)
CACHE_SECONDS = 60

_cache: dict = {}
_lock = threading.Lock()


def _responses(path: str) -> list[dict]:
    """The latest deliberate response on each finding in one instance's database, since its last Forget."""
    if not os.path.exists(path):
        return []
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        columns = {r[1] for r in db.execute("PRAGMA table_info(feedback)")}
        if "from_sentinel" not in columns:  # a database not yet opened by this version
            return []
        row = db.execute("SELECT value FROM state WHERE key = ?", (LEARN_SINCE,)).fetchone()
        since = int(row["value"]) if row else 0
        verdicts = (*ACTED_ON, *DISMISSED)
        marks = ",".join("?" * len(verdicts))
        rows = db.execute(
            f"""SELECT fb.verdict, fb.comment, fb.ts, f.category, f.title FROM feedback fb
                JOIN (SELECT finding_id, MAX(id) AS id FROM feedback
                      WHERE from_sentinel = 0 AND verdict IN ({marks}) AND ts > ? GROUP BY finding_id) l
                  ON l.id = fb.id
                JOIN findings f ON f.id = fb.finding_id""",
            (*verdicts, since),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        db.close()


def weight_of(acted: int, dismissed: int, points: int | None = None) -> float:
    """0.5 (always dismissed) to 1.5 (always acted on), 1 when there is too little to go on. `points` is `acted`
    with Important and Urgent counted more (`WEIGHTS`). The share is smoothed, so a few responses move the weight
    only a little."""
    if acted + dismissed < MIN_RESPONSES:
        return 1.0
    points = acted if points is None else points
    return round(0.5 + (points + 1) / (points + dismissed + 2), 2)


def learn(configs: list[Config]) -> dict:
    """Per category: how many findings the person acted on and dismissed, and the weight that gives; and the latest
    dismissal comments, newest first. Cached for a minute; `forget` and new responses clear the cache."""
    key = tuple(c.db_path for c in configs)
    with _lock:
        hit = _cache.get(key)
        if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
            return hit[1]
    responses = [{**r, "instance": c.name} for c in configs for r in _responses(c.db_path)]
    categories = {}
    for category in sorted(CATEGORIES):
        acted = [r["verdict"] for r in responses if r["category"] == category and r["verdict"] in ACTED_ON]
        dismissed = sum(1 for r in responses if r["category"] == category and r["verdict"] in DISMISSED)
        if acted or dismissed:
            points = sum(WEIGHTS[v] for v in acted)
            categories[category] = {"acted": len(acted), "urgent": acted.count("urgent"), "dismissed": dismissed,
                                    "weight": weight_of(len(acted), dismissed, points)}
    dismissals = sorted((r for r in responses if r["verdict"] in DISMISSED and r["comment"]),
                        key=lambda r: r["ts"], reverse=True)[:MAX_COMMENTS]
    comments = [{k: r[k] for k in ("instance", "category", "title", "comment")} for r in dismissals]
    result = {"categories": categories, "dismissal_comments": comments, "responses": len(responses)}
    with _lock:
        _cache[key] = (time.monotonic(), result)
    return result


def rank(finding, learned: dict) -> float:
    """The order a finding is shown in: its priority times its category's weight, never lowered for high impact."""
    weight = learned["categories"].get(finding["category"], {}).get("weight", 1.0)
    if finding["impact"] >= HIGH_IMPACT:
        weight = max(weight, 1.0)
    return round(finding["priority"] * weight, 3)


def forget(configs: list[Config]) -> None:
    """Ignore every response given until now, on every instance."""
    ts = now()
    for c in configs:
        store = Store(c.db_path)
        try:
            store.set_state(LEARN_SINCE, ts)
            store.commit()
        finally:
            store.close()
    clear()


def clear() -> None:
    with _lock:
        _cache.clear()
