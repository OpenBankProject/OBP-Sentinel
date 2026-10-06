"""The digest: the only thing Sentinel shows people. Its rules keep Sentinel from overwhelming them.

- It is written only after enough hours of watching since the last digest.
- It holds at most `digest_size` suggestions, and no more than `max_per_week` in any 7 days.
- A finding must clear `min_priority`, and its signatures must have shown up in more than one bucket.
- Nothing already suggested, accepted, acted on, dismissed or fixed is suggested again; "later" waits out its snooze.
- If nothing qualifies, the digest says so. That is a good outcome.
"""

import json
import os
from datetime import datetime, timezone

from .config import Config
from .store import Store, now
from .summary import watched_seconds

MIN_BUCKETS_FOR_SIGNATURE_FINDING = 2


class NotReady(Exception):
    pass


def _persistent(store: Store, signature_ids: list[str]) -> bool:
    if not signature_ids:
        return True  # telemetry-only findings rest on the summary's window comparison
    marks = ",".join("?" * len(signature_ids))
    buckets = store.db.execute(
        f"SELECT COUNT(DISTINCT bucket_start) FROM observations WHERE signature_id IN ({marks})", signature_ids
    ).fetchone()[0]
    return buckets >= MIN_BUCKETS_FOR_SIGNATURE_FINDING


def eligible_findings(store: Store, config: Config) -> list:
    ts = now()
    result = []
    for f in store.findings(("open", "later")):
        if f["status"] == "later" and (f["snoozed_until"] or 0) > ts:
            continue
        if f["priority"] < config.min_priority:
            continue
        if not _persistent(store, json.loads(f["signature_ids"])):
            continue
        result.append(f)
    return result


def write_digest(store: Store, config: Config, out_dir: str, force: bool = False) -> str:
    ts = now()
    since = store.get_state("last_digest_ts", 0)
    watched_hours = watched_seconds(store, since, ts, config.poll_seconds) / 3600
    if watched_hours < config.min_watch_hours and not force:
        raise NotReady(
            f"Only {watched_hours:.1f}h watched since the last digest; {config.min_watch_hours}h needed (use --force to override)"
        )
    budget = min(config.digest_size, config.max_per_week - store.suggestions_since(ts - 7 * 86400))
    candidates = eligible_findings(store, config)
    chosen = candidates[: max(budget, 0)]

    stamp = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d-%H%M")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{stamp}.md")
    lines = [
        f"# OBP-Sentinel digest {stamp} UTC: `{config.name}` ({config.obp_base_url})",
        "",
        f"Watched for {watched_hours:.1f}h since the last digest. "
        f"{len(candidates)} finding(s) qualified; showing {len(chosen)}.",
        "",
    ]
    if not chosen:
        lines.append("**Nothing worth your attention right now.**")
        if budget <= 0:
            lines.append(f"\n(The weekly limit of {config.max_per_week} suggestions has been reached.)")
    for n, f in enumerate(chosen, 1):
        files = json.loads(f["files"])
        lines += [
            f"## {n}. {f['title']}",
            "",
            f"**{f['category']}** · priority {f['priority']} "
            f"(impact {f['impact']}/5, confidence {f['confidence']:.0%}, effort {f['effort']}/5, trend {f['trend']}) "
            f"· finding #{f['id']} `{f['key']}`",
            "",
            "**Evidence.** " + f["evidence"],
            "",
            "**Likely cause.** " + f["hypothesis"],
            "",
        ]
        if files:
            lines += ["**Where.** " + ", ".join(f"`{p}`" for p in files), ""]
        lines += [
            "**Suggested fix.** " + f["suggested_fix"],
            "",
            f"Respond with `uv run sentinel feedback {f['id']} accepted|acted|dismissed|later|fixed --comment \"...\"`",
            "",
        ]
    with open(path, "w") as fh:
        fh.write("\n".join(lines))
    for f in chosen:
        store.record_suggestion(f["id"], path, ts)
    store.set_state("last_digest_ts", ts)
    store.commit()
    return path
