"""The OBP-API build on GitHub Actions (OBP_API_BUILD_ACTIONS_URL, e.g. https://github.com/<owner>/OBP-API/actions).

For the branch the OBP-API checkout is on, the latest run of each workflow: OK when all of them passed (or were
skipped), failing when one failed, with which workflow, at which commit, and which job and step, running while
one is still going. Asked of GitHub's API at most every CHECK_SECONDS, shared by the instances that name the same
URL. GITHUB_TOKEN, if set, is sent along: for a private repository, or a higher rate limit.
"""

import logging
import os
import re
import subprocess
import threading
import time

import httpx

from .config import Config

logger = logging.getLogger(__name__)

CHECK_SECONDS = 300
ACTIONS_URL = re.compile(r"^https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/actions/?$")
PASSED = {"success", "skipped", "neutral"}
_cache: dict[tuple[str, str | None], tuple[float, dict]] = {}
_lock = threading.Lock()


def _get(url: str) -> dict:
    headers = {"Accept": "application/vnd.github+json"}
    if token := os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    response = httpx.get(url, headers=headers, timeout=10)
    response.raise_for_status()
    return response.json()


def _branch(source: str) -> str | None:
    if not source:
        return None
    out = subprocess.run(["git", "-C", source, "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True)
    return out.stdout.strip() if out.returncode == 0 and out.stdout.strip() != "HEAD" else None


def status(config: Config) -> dict | None:
    """{state: ok | failing | running | unknown, text, url}, or None when no build is watched."""
    url = config.build_actions_url
    if not url:
        return None
    branch = _branch(config.obp_api_source)
    key = (url, branch)
    with _lock:
        cached = _cache.get(key)
        if cached and time.time() - cached[0] < CHECK_SECONDS:
            return cached[1]
    shown = check(url, branch)
    with _lock:
        _cache[key] = (time.time(), shown)
    return shown


def check(url: str, branch: str | None) -> dict:
    m = ACTIONS_URL.match(url)
    if not m:
        return {"state": "unknown", "text": f"OBP_API_BUILD_ACTIONS_URL is not a GitHub Actions page: {url}", "url": url}
    api = f"https://api.github.com/repos/{m['owner']}/{m['repo']}/actions"
    try:
        runs = _get(f"{api}/runs?per_page=30" + (f"&branch={branch}" if branch else ""))["workflow_runs"]
        latest = {}
        for run in runs:  # newest first: the first run of each workflow is its latest
            latest.setdefault(run["name"], run)
        on = f" on {branch}" if branch else ""
        if not latest:
            return {"state": "unknown", "text": f"No GitHub Actions runs{on} yet", "url": url}
        failed = [r for r in latest.values() if r["status"] == "completed" and r["conclusion"] not in PASSED]
        going = [r for r in latest.values() if r["status"] != "completed"]
        if failed:
            run = failed[0]
            jobs = _get(f"{api}/runs/{run['id']}/jobs?per_page=100")["jobs"]
            why = [f"job {j['name']} failed" + (f" at step {s['name']}" if (s := next(
                (s for s in j.get("steps", []) if s.get("conclusion") == "failure"), None)) else "")
                   for j in jobs if j.get("conclusion") not in PASSED | {None}]
            return {"state": "failing", "url": run["html_url"],
                    "text": f"{run['name']} {run['conclusion'].replace('_', ' ')} at {run['head_sha'][:9]}{on}"
                            + (f": {'; '.join(why)}" if why else "")}
        newest = max(latest.values(), key=lambda r: r["created_at"])
        if going:
            return {"state": "running", "url": going[0]["html_url"],
                    "text": f"{going[0]['name']} is running at {going[0]['head_sha'][:9]}{on}; the others passed"}
        return {"state": "ok", "url": newest["html_url"],
                "text": f"Build OK: the latest run of each workflow{on} passed, up to {newest['head_sha'][:9]}"}
    except (httpx.HTTPError, KeyError, ValueError) as e:
        logger.warning("Could not read the build at %s: %s", url, e)
        return {"state": "unknown", "text": f"Could not read the build from GitHub: {e}"[:300], "url": url}
