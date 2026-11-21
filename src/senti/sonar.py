"""SonarCloud as another source of findings (SONARCLOUD_PROJECT, e.g. OpenBankProject_OBP-API).

SonarCloud's analysis of a public project can be read without a login. SonarCloud lists thousands of issues for
OBP-API, nearly all of them about style. Senti keeps only a few kinds:
- security issues of medium severity or above, and security hotspots still to review of medium probability or above;
- reliability issues of high severity or above;
- every issue of medium severity or above that is new on the pull request for the branch the OBP-API checkout is on,
  while that pull request's analysis is newer than the main branch's.
Issues in test code are left out. The issues of one rule in one file make one finding, so twenty uses of `[` in a
script are one finding, not twenty. They become findings like any other, and the digest picks the few that matter.
A finding SonarCloud no longer reports is marked fixed, unless you have already acted on it or dismissed it.

Read every CHECK_SECONDS, for each project once, into the first instance that names it. SONARCLOUD_TOKEN, if set,
is sent along: for a private project.
"""

import hashlib
import logging
import os
import re
import subprocess
import threading
import time

import httpx

from .config import Config
from .store import Store

logger = logging.getLogger(__name__)

API = "https://sonarcloud.io/api"
CHECK_SECONDS = 3600  # SonarCloud analyses on each push, so an hour is soon enough
PAGE_SIZE = 500
MAX_PAGES = 4  # 2000 issues: the filters keep far fewer than that
KEY_PREFIX = "sonar:"
TEST_CODE = re.compile(r"(^|/)src/test/")
MAIN_CODE = re.compile(r"(^|/)src/main/")  # elsewhere (scripts, workflows, Docker files) counts for one less
IMPACT = {"BLOCKER": 5, "HIGH": 4, "MEDIUM": 3, "LOW": 2, "INFO": 1}
CATEGORY = {"SECURITY": "security", "RELIABILITY": "reliability", "MAINTAINABILITY": "readability"}
FILE_NAMES_LISTED = 12  # lines named in a finding's files, at most
ORIGIN_OWNER = re.compile(r"github\.com[:/](?P<owner>[^/]+)/")


def _get(path: str, params: dict) -> dict:
    headers = {"Authorization": f"Bearer {token}"} if (token := os.environ.get("SONARCLOUD_TOKEN")) else {}
    response = httpx.get(f"{API}/{path}", params=params, headers=headers, timeout=30)
    response.raise_for_status()
    return response.json()


def _git(source: str, *args: str) -> str:
    out = subprocess.run(["git", "-C", source, *args], capture_output=True, text=True)
    return out.stdout.strip() if out.returncode == 0 else ""


def pull_request(project: str, source: str) -> dict | None:
    """The pull request for the branch the checkout is on, by its origin's owner (owner:branch), when SonarCloud
    analysed it after the main branch; else None (it is merged, or the main branch has caught up)."""
    branch = _git(source, "rev-parse", "--abbrev-ref", "HEAD") if source else ""
    if not branch or branch == "HEAD":
        return None
    m = ORIGIN_OWNER.search(_git(source, "remote", "get-url", "origin"))
    names = {branch} | ({f"{m['owner']}:{branch}"} if m else set())
    prs = [p for p in _get("project_pull_requests/list", {"project": project}).get("pullRequests", [])
           if p.get("branch") in names and p.get("analysisDate")]
    if not prs:
        return None
    latest = max(prs, key=lambda p: p["analysisDate"])
    main = next((b for b in _get("project_branches/list", {"project": project}).get("branches", []) if b.get("isMain")), {})
    return latest if latest["analysisDate"] > main.get("analysisDate", "") else None


def _issues(project: str, extra: dict) -> list[dict]:
    found = []
    for page in range(1, MAX_PAGES + 1):
        data = _get("issues/search", {"componentKeys": project, "issueStatuses": "OPEN,CONFIRMED",
                                      "ps": PAGE_SIZE, "p": page, **extra})
        found += data.get("issues", [])
        if page * PAGE_SIZE >= data.get("total", 0):
            break
    return found


def _kept(issue: dict) -> bool:
    return any((i["softwareQuality"] == "SECURITY" and IMPACT[i["severity"]] >= 3)
               or (i["softwareQuality"] == "RELIABILITY" and IMPACT[i["severity"]] >= 4)
               for i in issue.get("impacts", []))


def _worst(impacts: list[dict]) -> dict:
    order = ["SECURITY", "RELIABILITY", "MAINTAINABILITY"]
    return max(impacts, key=lambda i: (IMPACT[i["severity"]], -order.index(i["softwareQuality"])))


def fetch(project: str, source: str = "") -> tuple[list[dict], dict | None]:
    """The issues and hotspots Senti keeps, as {path, line, rule, message, quality, severity, url, where},
    and the pull request read, if any."""
    def item(path, line, rule, message, quality, severity, url, where):
        return {"path": path, "line": line, "rule": rule, "message": message, "quality": quality,
                "severity": severity, "url": url, "where": where}

    def path_of(component: str) -> str:
        return component.split(":", 1)[1] if ":" in component else component

    page = f"https://sonarcloud.io/project/issues?id={project}"
    kept = []
    for issue in _issues(project, {"impactSoftwareQualities": "SECURITY,RELIABILITY",
                                   "impactSeverities": "MEDIUM,HIGH,BLOCKER"}):
        if _kept(issue) and not TEST_CODE.search(path := path_of(issue["component"])):
            worst = _worst(issue["impacts"])
            kept.append(item(path, issue.get("line"), issue["rule"], issue["message"], worst["softwareQuality"],
                             worst["severity"], f"{page}&open={issue['key']}", "the main branch"))
    hotspots = _get("hotspots/search", {"projectKey": project, "status": "TO_REVIEW", "ps": PAGE_SIZE}).get("hotspots", [])
    for spot in hotspots:
        if IMPACT[spot["vulnerabilityProbability"]] >= 3 and not TEST_CODE.search(path := path_of(spot["component"])):
            kept.append(item(path, spot.get("line"), spot["ruleKey"], spot["message"], "SECURITY",
                             spot["vulnerabilityProbability"],
                             f"https://sonarcloud.io/project/security_hotspots?id={project}&hotspots={spot['key']}",
                             "the main branch (a security hotspot to review)"))
    pr = pull_request(project, source)
    if pr:
        for issue in _issues(project, {"pullRequest": pr["key"]}):
            worst = _worst(issue["impacts"]) if issue.get("impacts") else None
            if worst and IMPACT[worst["severity"]] >= 3 and not TEST_CODE.search(path := path_of(issue["component"])):
                kept.append(item(path, issue.get("line"), issue["rule"], issue["message"], worst["softwareQuality"],
                                 worst["severity"], f"{page}&pullRequest={pr['key']}&open={issue['key']}",
                                 f"pull request {pr['key']}, new there"))
    return kept, pr


def findings(project: str, kept: list[dict]) -> list[dict]:
    """One finding per rule and file, worst severity first."""
    groups: dict[tuple[str, str], list[dict]] = {}
    for k in kept:
        groups.setdefault((k["rule"], k["path"]), []).append(k)
    made = []
    for (rule, path), items in groups.items():
        items.sort(key=lambda k: (-IMPACT[k["severity"]], k["line"] or 0))
        worst, lines = items[0], sorted({k["line"] for k in items if k["line"]})
        new = any(k["where"].startswith("pull request") for k in items)
        impact = max(1, IMPACT[worst["severity"]] - (0 if MAIN_CODE.search(path) else 1))
        name = path.rsplit("/", 1)[-1]
        places = f" ({len(items)} places)" if len(items) > 1 else ""
        title = worst["message"].split(". ")[0].rstrip(".")  # the first sentence
        evidence = "\n".join(
            [f"SonarCloud ({project}) reports, by its rule {rule}, {worst['quality'].lower()} "
             f"{worst['severity'].lower()}:"]
            + [f"- {k['path']}:{k['line'] or '?'} on {k['where']}: {k['message']} {k['url']}" for k in items])
        made.append({
            "key": KEY_PREFIX + hashlib.sha1(f"{project}|{rule}|{path}".encode()).hexdigest()[:16],
            "category": CATEGORY.get(worst["quality"], "readability"),
            "title": f"{title} in {name}{places}",
            "summary": f"SonarCloud's rule {rule} flags {name}{places}. Check the code to see whether it is a real "
                       f"problem before fixing it.",
            "evidence": evidence,
            "hypothesis": f"SonarCloud's rule {rule} matches here. Static analysis can be wrong: read the code first.",
            "files": [f"{path}:{line}" for line in lines[:FILE_NAMES_LISTED]] or [path],
            "signature_ids": [],
            "suggested_fix": f"{worst['message']} See SonarCloud's explanation of rule {rule} on the issue page above.",
            "impact": impact, "confidence": 0.4 if "hotspot" in worst["where"] else 0.6,
            "effort": 1 if len(items) <= 3 else 2, "trend": "new" if new else "stable"})
    return made


def record(config: Config, project: str, made: list[dict]) -> dict:
    """Add or update the findings; mark fixed those from SonarCloud it no longer reports, unless acted on or
    dismissed. Returns how many were added, updated and marked fixed."""
    store = Store(config.db_path)
    try:
        keys = {f["key"] for f in made}
        before = {r["key"] for r in store.db.execute("SELECT key FROM findings WHERE key LIKE ?", (KEY_PREFIX + "%",))}
        for f in made:
            store.upsert_finding(f)
        gone = [r["id"] for r in store.db.execute(
            "SELECT id, key FROM findings WHERE key LIKE ? AND evidence LIKE ? "
            "AND status IN ('open', 'suggested', 'accepted', 'later')",
            (KEY_PREFIX + "%", f"SonarCloud ({project})%")) if r["key"] not in keys]
        for finding_id in gone:
            store.add_feedback(finding_id, "fixed", "SonarCloud no longer reports it.", 0, from_sentinel=True)
        store.commit()
    finally:
        store.close()
    return {"added": len(keys - before), "updated": len(keys & before), "fixed": len(gone)}


def check(config: Config) -> dict:
    kept, pr = fetch(config.sonarcloud_project, config.obp_api_source)
    done = record(config, config.sonarcloud_project, findings(config.sonarcloud_project, kept))
    return {**done, "pull_request": pr["key"] if pr else None}


def instances(configs: list[Config]) -> list[Config]:
    """For each SonarCloud project, the first instance that names it."""
    seen, chosen = set(), []
    for c in configs:
        if c.sonarcloud_project and c.sonarcloud_project not in seen:
            seen.add(c.sonarcloud_project)
            chosen.append(c)
    return chosen


def check_forever(configs: list[Config], stopped: threading.Event) -> None:
    delay = 30  # the first check soon after Senti starts
    while not stopped.wait(delay):
        delay = CHECK_SECONDS
        for config in instances(configs):
            try:
                done = check(config)
                logger.info("SonarCloud %s (%s): %s", config.sonarcloud_project, config.name, done)
            except (httpx.HTTPError, ValueError, KeyError) as e:
                logger.warning("Could not read SonarCloud %s: %s", config.sonarcloud_project, e)


# --- Suggested SonarCloud settings, for the panel on the page ------------------------------------------------------
#
# SonarCloud analyses a project in one of two ways. Automatic analysis (no scanner in CI) reads .sonarcloud.properties;
# a scanner run in CI reads sonar-project.properties. Settings in the other file are not used. Senti suggests the
# file SonarCloud reads, so that test code counts as tests and developer scripts are left out, and says how many
# issues each line moves. It never writes the file: the person copies it, and commits it if they agree.

SETTINGS_SECONDS = 3600  # read from SonarCloud at most once an hour per project
SET_HERE = ("sonar.sources", "sonar.tests", "sonar.exclusions", "sonar.test.inclusions")
DEV_ONLY = ("scripts",)  # top-level folders of developer tools, not part of what is built or run
METRICS = ("vulnerabilities", "bugs", "security_hotspots", "code_smells")
_settings: dict[str, tuple[float, dict]] = {}


def _show(source: str, path: str) -> str | None:
    out = subprocess.run(["git", "-C", source, "show", f"HEAD:{path}"], capture_output=True, text=True)
    return out.stdout if out.returncode == 0 else None


def _scanner_in_ci(source: str) -> bool:
    """True when a GitHub workflow at HEAD runs a SonarCloud or SonarQube scanner."""
    out = subprocess.run(["git", "-C", source, "grep", "-il", "-e", "sonarcloud", "-e", "sonarqube", "-e", "sonar-scanner",
                          "-e", "sonar:sonar", "HEAD", "--", ".github/workflows"], capture_output=True, text=True)
    return out.returncode == 0 and bool(out.stdout.strip())


def _counts(project: str, path: str) -> dict[str, int]:
    """SonarCloud's open issue counts in one folder; zeros if it does not know the folder."""
    try:
        data = _get("measures/component", {"component": f"{project}:{path}", "metricKeys": ",".join(METRICS)})
    except httpx.HTTPStatusError:
        return dict.fromkeys(METRICS, 0)
    got = {m["metric"]: int(float(m.get("value", 0))) for m in data.get("component", {}).get("measures", [])}
    return {m: got.get(m, 0) for m in METRICS}


def _properties(text: str | None) -> dict[str, str]:
    found = {}
    for line in (text or "").splitlines():
        if "=" in line and not line.lstrip().startswith(("#", "!")):
            key, value = line.split("=", 1)
            found[key.strip()] = value.strip()
    return found


def _kept_lines(text: str | None) -> str:
    """A properties file without the settings Senti sets, its comments kept with the lines they describe."""
    kept, comment = [], []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith(("#", "!")) or not stripped:
            comment.append(line)
            continue
        if line.split("=", 1)[0].strip() in SET_HERE:
            comment = []
            continue
        kept += comment + [line]
        comment = []
    return "\n".join(kept).strip()


def suggest(project: str, source: str) -> dict:
    """The file SonarCloud reads for this project, as it is and as Senti suggests it, with what each change does."""
    ci = _scanner_in_ci(source)
    reads, other = ("sonar-project.properties", ".sonarcloud.properties") if ci else (".sonarcloud.properties",
                                                                                         "sonar-project.properties")
    current = {reads: _show(source, reads), other: _show(source, other)}
    top = _git(source, "ls-tree", "-d", "--name-only", "HEAD").splitlines()
    test_dirs = [f"{d}/src/test" for d in top if _git(source, "ls-tree", "-d", "--name-only", "HEAD", f"{d}/src/test")]
    dev_dirs = [d for d in DEV_ONLY if d in top]
    known_tests = _get("components/tree", {"component": project, "qualifiers": "UTS", "ps": 1})["paging"]["total"]

    def total(paths):
        sums = dict.fromkeys(METRICS, 0)
        for path in paths:
            for metric, n in _counts(project, path).items():
                sums[metric] += n
        return sums

    was = _properties(current[reads] or current[other])  # its own exclusions are kept
    exclusions = [e for e in (was.get("sonar.exclusions", "").split(",")) if e.strip()]
    exclusions += [f"{d}/**" for d in dev_dirs if f"{d}/**" not in exclusions]
    effects = []
    if test_dirs:
        effects.append({"lines": ["sonar.tests=.", "sonar.test.inclusions=**/src/test/**"],
                        "what": f"Counts {', '.join(test_dirs)} as test code, which SonarCloud checks with its rules for "
                                f"tests: most security rules, such as hard-coded passwords, do not apply there.",
                        "counts": total(test_dirs)})
    if dev_dirs or test_dirs:
        effects.append({"lines": [f"sonar.exclusions={','.join(exclusions + ['**/src/test/**'])}"],
                        "what": (f"Leaves {', '.join(d + '/' for d in dev_dirs)} out: developer tools run by hand, not "
                                 "part of what is built or run. " if dev_dirs else "")
                                + "Test code is left out of the main code, so it is not checked twice.",
                        "counts": total(dev_dirs)})
    lines = [f"# Read by SonarCloud's {'scanner in CI' if ci else 'automatic analysis'}.",
             "# Main code is everything but the tests and the developer tools; tests are checked as tests.",
             "sonar.sources=.", effects[-1]["lines"][0] if effects else "sonar.exclusions=**/src/test/**"]
    if test_dirs:
        lines += effects[0]["lines"]
    kept = _kept_lines(current[reads] or current[other])
    suggested = "\n".join(lines) + ("\n\n" + kept if kept else "") + "\n"
    notes = []
    if current[other] and not current[reads]:
        notes.append(f"SonarCloud {'runs from a workflow' if ci else 'analyses this project by itself (no workflow runs a scanner)'}"
                     f", so it reads {reads}, not {other}. The settings in {other} are not used: "
                     f"SonarCloud knows {known_tests} test file{'s' if known_tests != 1 else ''}. "
                     f"The suggestion keeps them and puts them in {reads}.")
    if "sonar.issue.ignore.multicriteria" in suggested and not ci:
        notes.append("If SonarCloud still reports the issues the sonar.issue.ignore lines leave out, set them in SonarCloud "
                     "instead: Administration, Analysis Scope, Ignore Issues on Multiple Criteria.")
    return {"project": project, "reads": reads, "ci": ci, "current": current, "suggested": suggested,
            "effects": effects, "notes": notes, "known_test_files": known_tests,
            "changed": (current[reads] or "").strip() != suggested.strip()}


def settings(config: Config) -> dict | None:
    """suggest() for an instance's SonarCloud project, kept for SETTINGS_SECONDS; None if it names none."""
    project = config.sonarcloud_project
    if not project or not config.obp_api_source:
        return None
    at, made = _settings.get(project, (0.0, None))
    if made is None or time.time() - at > SETTINGS_SECONDS:
        made = suggest(project, config.obp_api_source)
        _settings[project] = (time.time(), made)
    return made
