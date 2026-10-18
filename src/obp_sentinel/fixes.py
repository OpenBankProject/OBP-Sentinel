"""What was fixed since Sentinel noticed it, without anyone having to mark it.

Two signals. A finding's log signatures went quiet: none seen since a new commit was deployed, while Sentinel
kept watching for QUIET_HOURS (no LLM). Then Look again, started by itself on the analyst's schedule, reads the
source at the running commit: if Claude finds it fixed, the finding is marked fixed, with the fixing commit in
Claude's answer. Shown on the Fixed page.
"""

import json
import logging
import threading

from . import recheck
from .config import Config
from .store import Store, now
from .summary import watched_seconds

logger = logging.getLogger(__name__)

QUIET_HOURS = 24
LOOKS_PER_CHECK = 3  # Look again costs up to recheck.BUDGET_USD each: a few per hour drains a backlog slowly
_checking: set[str] = set()  # instances whose quiet findings are being looked at again
_lock = threading.Lock()


def quiet(store: Store, config: Config) -> list[dict]:
    """Findings, not yet fixed or dismissed, whose log signatures were all last seen before a deployment that
    Sentinel has watched for QUIET_HOURS since. Newest deployment first."""
    deployments = store.db.execute(
        "SELECT git_commit, first_seen FROM deployments WHERE git_commit IS NOT NULL ORDER BY first_seen").fetchall()
    result = []
    for f in store.db.execute("SELECT * FROM findings WHERE status NOT IN ('fixed', 'dismissed')"):
        ids = json.loads(f["signature_ids"])
        if not ids:
            continue  # found by reading the code: no logs to go quiet
        marks = ",".join("?" * len(ids))
        seen = store.db.execute(f"SELECT COUNT(*), MAX(last_seen), SUM(total_count) FROM signatures WHERE id IN ({marks})",
                                ids).fetchone()
        if seen[0] != len(ids):
            continue
        last_seen = seen[1]
        since = next((d for d in deployments if d["first_seen"] > last_seen), None)
        if not since or watched_seconds(store, since["first_seen"], now(), config.poll_seconds) < QUIET_HOURS * 3600:
            continue
        looked = (f["rechecked_at"] or 0) >= since["first_seen"]
        result.append({"id": f["id"], "title": f["title"], "summary": f["summary"], "category": f["category"],
                       "created_at": f["created_at"], "last_seen": last_seen, "times_seen": seen[2],
                       "commit": since["git_commit"], "deployed_at": since["first_seen"],
                       "recheck": f["recheck"] if looked else None, "recheck_status": f["recheck_status"],
                       "looked": looked})
    return sorted(result, key=lambda q: -q["deployed_at"])


def fixed(store: Store) -> list[dict]:
    """Findings marked fixed, by Look again or by hand, with when and the comment. Newest first."""
    rows = store.db.execute(
        """SELECT f.id, f.title, f.summary, f.category, f.created_at, b.ts AS fixed_at, b.comment
           FROM findings f JOIN feedback b ON b.id = (SELECT MAX(id) FROM feedback WHERE finding_id = f.id)
           WHERE f.status = 'fixed' ORDER BY b.ts DESC""").fetchall()
    return [{**dict(r), "by": "Look again" if (r["comment"] or "").startswith("Look again:") else "by hand"}
            for r in rows]


def overview(store: Store, config: Config) -> dict:
    """For the Fixed page."""
    return {"fixed": fixed(store), "quiet": quiet(store, config), "quiet_hours": QUIET_HOURS}


def check(config: Config) -> None:
    """Look again, in the background, at a few quiet findings not looked at since they went quiet. Those Claude
    finds fixed are marked fixed. One check at a time per instance."""
    with _lock:
        if config.name in _checking:
            return
        _checking.add(config.name)
    try:
        store = Store(config.db_path)
        try:
            ids = [q["id"] for q in quiet(store, config) if not q["looked"]][:LOOKS_PER_CHECK]
        finally:
            store.close()
    except BaseException:
        with _lock:
            _checking.discard(config.name)
        raise
    threading.Thread(target=_look, args=(config, ids), daemon=True, name=f"fixes-{config.name}").start()


def _look(config: Config, ids: list[int]) -> None:
    try:
        for finding_id in ids:
            if recheck.look_and_mark(config, finding_id):
                logger.info("Finding %s (%s) went quiet and Look again found it fixed", finding_id, config.name)
    finally:
        with _lock:
            _checking.discard(config.name)
