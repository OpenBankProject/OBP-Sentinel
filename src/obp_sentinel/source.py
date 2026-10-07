"""Source review: which OBP-API endpoints can be reached on the watched instances, in what order the analyst
reviews their code, and what it has reviewed.

Reachable, in order: endpoints that need no login, endpoints that need a login but no Role, endpoints whose Role
someone holds (GET /reachable-roles),
then endpoints whose Roles are unknown because the instance does not offer reachable-roles, and last
endpoints that need a Role nobody holds (someone may be granted it later). How often an endpoint is called does not matter here: for security what
counts is who can reach it.

What was reviewed is kept in one database for all instances (SENTINEL_SOURCE_DB), by git blob: a file whose
content has not changed is never reviewed twice, whatever the commit or instance, and survives restarts.
"""

import hashlib
import json
import re
import sqlite3
import subprocess
from functools import lru_cache

from .config import Config
from .obp_client import OBPClient
from .store import Store, now

SOURCE_DIR = "obp-api/src/main/scala"
REACH_EVERY_SECONDS = 86400  # resource docs are large: read them once a day, or when the commit changes
ENDPOINT_VAL = re.compile(r"\s*(lazy\s+)?val\s+\w+\s*:\s*(HttpRoutes|OBPEndpoint)\b")
TIERS = ("no login needed", "login but no Role needed", "Role held", "Roles unknown", "Role nobody holds")
# In a ResourceDoc's errors, these mean the caller must be logged in or an identified application
CREDENTIALS_ERRORS = ("OBP-20001", "OBP-20200")
REACH_FORMAT = 3  # bump when what fetch_reach stores changes, so it is fetched again

SCHEMA = """
CREATE TABLE IF NOT EXISTS reviewed_files (
    blob         TEXT PRIMARY KEY,         -- git blob id: the file's content
    path         TEXT NOT NULL,
    git_commit   TEXT NOT NULL,
    reviewed_at  INTEGER NOT NULL,
    operation_id TEXT NOT NULL             -- the endpoint it was read for
);
CREATE TABLE IF NOT EXISTS reviewed_endpoints (
    operation_id TEXT PRIMARY KEY,
    git_commit   TEXT NOT NULL,
    reviewed_at  INTEGER NOT NULL,
    files        TEXT NOT NULL             -- JSON object: path -> blob, as read
);
"""


# --- collecting: what each instance exposes --------------------------------------------

def fetch_reach(client: OBPClient, git_commit: str | None) -> dict:
    """Every endpoint the instance serves with the Roles it needs and whether it needs a login, and the Roles
    anyone holds (None if unknown)."""
    endpoints: dict[str, dict] = {}
    versions = [v["fully_qualified_version"] for v in client.api_versions()
                if v.get("is_active") and not v["api_short_version"].startswith("dynamic")]
    for version in versions:
        for doc in client.resource_docs(version):
            errors = " ".join(str(e) for e in doc.get("error_response_bodies") or [])
            endpoints[doc["operation_id"]] = {"roles": sorted({r["role"] for r in doc.get("roles") or []}),
                                              "login": any(code in errors for code in CREDENTIALS_ERRORS)}
    try:
        roles = client.reachable_roles()
    except Exception:
        roles = None  # an instance without the endpoint, or without the Scope
    return {"format": REACH_FORMAT, "ts": now(), "git_commit": git_commit, "versions": versions,
            "endpoints": endpoints, "roles": roles}


def reach_is_due(store: Store, git_commit: str | None) -> bool:
    reach = store.get_state("reach")
    return (not reach or reach.get("format") != REACH_FORMAT or reach["git_commit"] != git_commit
            or now() - reach["ts"] > REACH_EVERY_SECONDS)


# --- what was reviewed --------------------------------------------------------------------

class Reviews:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def endpoint(self, operation_id: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM reviewed_endpoints WHERE operation_id = ?", (operation_id,)).fetchone()

    def file(self, blob: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM reviewed_files WHERE blob = ?", (blob,)).fetchone()

    def last_review_of(self, path: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM reviewed_files WHERE path = ? ORDER BY reviewed_at DESC LIMIT 1", (path,)).fetchone()

    def record(self, operation_id: str, git_commit: str, files: dict[str, str]) -> None:
        """files: path -> fingerprint, as read for this endpoint."""
        ts = now()
        self.db.execute(
            "INSERT OR REPLACE INTO reviewed_endpoints (operation_id, git_commit, reviewed_at, files) VALUES (?, ?, ?, ?)",
            (operation_id, git_commit, ts, json.dumps(files, sort_keys=True)))
        for path, blob in files.items():
            if blob.startswith("code:"):
                continue  # only one endpoint's code in that file was read: the file is not reviewed
            self.db.execute(
                "INSERT OR IGNORE INTO reviewed_files (blob, path, git_commit, reviewed_at, operation_id) VALUES (?, ?, ?, ?, ?)",
                (blob, path, git_commit, ts, operation_id))
        self.db.commit()

    def close(self) -> None:
        self.db.close()


# --- git ------------------------------------------------------------------------------------

def _git(source: str, *args: str) -> str | None:
    out = subprocess.run(["git", "-C", source, *args], capture_output=True, text=True, timeout=30)
    return out.stdout.strip() if out.returncode == 0 else None


def readable_commit(source: str, git_commit: str | None) -> str:
    """The commit to read: the deployed one if the checkout has it, else the checkout's HEAD."""
    if git_commit and _git(source, "cat-file", "-e", f"{git_commit}^{{commit}}") is not None:
        return git_commit
    return _git(source, "rev-parse", "HEAD") or "HEAD"


def blob_of(source: str, commit: str, path: str) -> str | None:
    return _git(source, "rev-parse", f"{commit}:{path}")


@lru_cache(maxsize=64)
def _show(source: str, commit: str, path: str) -> str | None:
    return _git(source, "show", f"{commit}:{path}")


def fingerprint(source: str, commit: str, path: str, operation_id: str) -> str | None:
    """What says whether a file read for an endpoint has changed. Most files: their git blob. The file the
    endpoint is defined in holds many endpoints, so there only the endpoint's own code counts: from its
    `val` to the next endpoint's (its ResourceDoc included)."""
    function = operation_id.rpartition("-")[2]
    text = _show(source, commit, path)
    if text is None:
        return None
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if re.match(rf"\s*(lazy\s+)?val\s+{re.escape(function)}\b", line)), None)
    if start is None or f"nameOf({function})" not in text:
        return blob_of(source, commit, path)
    end = next((i for i in range(start + 1, len(lines)) if ENDPOINT_VAL.match(lines[i])), len(lines))
    return "code:" + hashlib.sha1("\n".join(lines[start:end]).encode()).hexdigest()


def handler_files(source: str, commit: str, operation_id: str) -> list[str]:
    """Where the endpoint is defined: the files that name its function in a ResourceDoc."""
    version, _, function = operation_id.rpartition("-")
    out = _git(source, "grep", "-l", "-F", f"nameOf({function})", commit, "--", SOURCE_DIR)
    files = [line.split(":", 1)[1] for line in (out or "").splitlines()]
    # The same function name is in several versions' files (root, for one): keep the endpoint's own version's
    package = "/" + version[version.find("v"):].replace(".", "_") + "/"  # OBPv5.1.0 -> /v5_1_0/, BGv1.3 -> /v1_3/
    return [f for f in files if package in f] or files


# --- the queue --------------------------------------------------------------------------------

def tier_of(endpoint: dict, held: list[str] | None) -> int:
    if not endpoint["roles"]:
        return 1 if endpoint["login"] else 0
    if held is None:
        return 3
    return 2 if set(endpoint["roles"]) & set(held) else 4


def reachable(configs: list[Config]) -> dict[str, dict]:
    """Every endpoint any instance serves, with its best tier and where it was seen."""
    out: dict[str, dict] = {}
    for config in configs:
        store = Store(config.db_path)
        try:
            reach = store.get_state("reach")
        finally:
            store.close()
        if not reach or reach.get("format") != REACH_FORMAT:
            continue
        for op, endpoint in reach["endpoints"].items():
            tier = tier_of(endpoint, reach["roles"])
            e = out.setdefault(op, {"operation_id": op, "tier": tier, "instances": [],
                                    "source": config.obp_api_source, "git_commit": reach["git_commit"]})
            if tier < e["tier"]:
                e.update(tier=tier, source=config.obp_api_source, git_commit=reach["git_commit"])
            e["instances"].append(config.name)
    return out


def changed_files(reviews: Reviews, e: dict) -> list[str] | None:
    """None if never reviewed; else the files read for it whose content is different now."""
    row = reviews.endpoint(e["operation_id"])
    if not row:
        return None
    commit = readable_commit(e["source"], e["git_commit"])
    return [path for path, mark in json.loads(row["files"]).items()
            if fingerprint(e["source"], commit, path, e["operation_id"]) != mark]


def progress(configs: list[Config], reviews: Reviews, recent: int = 8) -> dict:
    """For the web page: how many endpoints are reviewed, overall and per tier, the latest reviews, and per
    instance what was scanned: its API versions, endpoints and how many Roles are held there."""
    tiers = [{"label": label, "total": 0, "reviewed": 0} for label in TIERS]
    for e in reachable(configs).values():
        tiers[e["tier"]]["total"] += 1
        if changed_files(reviews, e) == []:
            tiers[e["tier"]]["reviewed"] += 1
    rows = reviews.db.execute(
        "SELECT operation_id, git_commit, reviewed_at FROM reviewed_endpoints ORDER BY reviewed_at DESC LIMIT ?",
        (recent,)).fetchall()
    return {"total": sum(t["total"] for t in tiers), "reviewed": sum(t["reviewed"] for t in tiers),
            "tiers": [t for t in tiers if t["total"]], "recent": [dict(r) for r in rows],
            "instances": [scanned(config) for config in configs]}


def scanned(config: Config) -> dict:
    store = Store(config.db_path)
    try:
        reach = store.get_state("reach")
    finally:
        store.close()
    if not reach or reach.get("format") != REACH_FORMAT:
        return {"name": config.name, "listed_at": None}
    return {"name": config.name, "listed_at": reach["ts"], "git_commit": reach["git_commit"],
            "versions": reach["versions"], "endpoints": len(reach["endpoints"]),
            "roles": len(reach["roles"]) if reach["roles"] is not None else None}


def next_endpoints(configs: list[Config], reviews: Reviews, n: int) -> tuple[list[dict], int, int]:
    """The next n endpoints to review, with how many endpoints there are and how many are reviewed."""
    endpoints = sorted(reachable(configs).values(), key=lambda e: (e["tier"], e["operation_id"]))
    picked, reviewed = [], 0
    for e in endpoints:
        changed = changed_files(reviews, e)
        if changed == []:
            reviewed += 1
            continue
        if len(picked) < n:
            commit = readable_commit(e["source"], e["git_commit"])
            picked.append({**e, "read_at": commit, "changed": changed,
                           "handlers": handler_files(e["source"], commit, e["operation_id"])})
    return picked, len(endpoints), reviewed
