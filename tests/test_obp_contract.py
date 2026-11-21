"""The contract between Sentinel and OBP-API.

`obp-api-contract/contract.json` lists every OBP-API call Sentinel makes, with the response fields the code reads.
`obp-api-contract/snapshot.json` is what an OBP-API documents for those calls, written by
`obp-api-contract/snapshot.py` from its resource docs. These tests need no server: they check that the list matches
the code, and that every call reaches an endpoint OBP-API documents, with every field the code reads. Retake the
snapshot from a new OBP-API build to learn whether that build breaks Sentinel.
"""

import ast
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONTRACT = ROOT / "obp-api-contract"
SOURCE = ROOT / "src"
# (file, fragment) -> why a line that names an OBP-API path is not a call of its own
NOT_CALLS = {
    ("src/obp_sentinel/obp_client.py", 'f"{self.config.obp_base_url}/obp/{version}/{path}"'):
        "the helper every self._request call goes through; each of those calls is listed by its own line",
    ("src/obp_sentinel/signatures.py", "OBP_PATH_RE = re.compile("):
        "a pattern that finds OBP-API paths in log lines",
    ("src/obp_sentinel/health.py", "OBP_VERSION_PATH = re.compile("):
        "a pattern that finds OBP-API paths in the error messages the health page shows",
    ("src/obp_sentinel/obp_client.py", "{self.config.monitoring_path}/log-cache/{level}"):
        "an app's own monitoring endpoint (see about in contract.json), not an OBP-API call",
    ("src/obp_sentinel/obp_client.py", "{self.config.monitoring_path}/telemetry"):
        "an app's own monitoring endpoint (see about in contract.json), not an OBP-API call",
    ("src/obp_sentinel/health.py", "expects a different answer from OBP-API's GET /obp/v7.0.0/banks/SYS/my/roles/ROLE"):
        "advice shown on the health page; the call itself is listed in obp_client.py",
}
VERSION = re.compile(r"/obp/(v\d+\.\d+\.\d+)/")
# The OBPClient methods that send a request to an OBP-API path (_request) or to a URL (_request_url)
CALL_HELPERS = {"_request", "_request_url"}


def load(name: str) -> dict:
    return json.loads((CONTRACT / name).read_text(encoding="utf-8"))


def calls() -> list[dict]:
    return load("contract.json")["calls"]


def key(call: dict) -> str:
    return f"{call['verb']} {call['path']}"


def documented(call: dict, snapshot: dict) -> dict | None:
    return next((e for e in snapshot["endpoints"] if key(e) == key(call)), None)


def undocumented_reads(call: dict, fields: set[str]) -> list[str]:
    """The reads of `call` that `fields` does not document. A read written `a|b` is satisfied by either field."""
    return [read for read in call.get("reads", []) if not any(f in fields for f in read.split("|"))]


def _docstrings(tree: ast.AST) -> set[int]:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                ids.add(id(first.value))
    return ids


# A string naming an OBP-API path or the start of one: /obp/v7.0.0/..., /obp/{version}/..., /obp/dynamic-entity/...
# (no version at all), or a piece such as "/obp/" put together with `+`. /obp-oidc is not an OBP-API path.
OBP_PATH_PIECE = re.compile(r"/obp(?![\w-])")


def obp_path_lines_in(rel: str, text: str) -> list[tuple[str, int, str]]:
    """Lines of Python source that make an OBP-API call: a string (not a docstring or comment) naming an OBP-API
    path or a piece of one, or a call of OBPClient._request or _request_url (which put /obp/{version}/ in front of
    their path, or take a whole URL). For a _request_url call whose URL is a variable, the line that sets the variable
    is part of the call. (file relative to the repository, line number, line)."""
    out = set()
    lines = text.splitlines()
    tree = ast.parse(text)
    skip = _docstrings(tree)
    assigned = {}  # variable -> (line number, line) of its latest assignment so far
    for node in sorted((n for n in ast.walk(tree) if hasattr(n, "lineno")), key=lambda n: n.lineno):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            assigned[node.targets[0].id] = lines[node.lineno - 1]
        if isinstance(node, ast.JoinedStr) or (isinstance(node, ast.Constant) and isinstance(node.value, str)
                                               and id(node) not in skip):
            # A string can run over several lines: take the line that names the path. Look at the string's own
            # text, not the rest of its line (a comment after it)
            segment = (ast.get_source_segment(text, node) or "").splitlines()
            out.update((rel, node.lineno + i, lines[node.lineno + i - 1]) for i, part in enumerate(segment)
                       if OBP_PATH_PIECE.search(part))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in CALL_HELPERS:
            line = lines[node.lineno - 1]
            url = node.args[1] if node.func.attr == "_request_url" and len(node.args) > 1 else None
            if isinstance(url, ast.Name) and url.id in assigned:
                line += " " + assigned[url.id].strip()
            out.add((rel, node.lineno, line))
    return sorted(out)


def obp_path_lines() -> list[tuple[str, int, str]]:
    out = []
    for path in sorted(SOURCE.rglob("*.py")):
        if "__pycache__" not in path.parts:
            out += obp_path_lines_in(path.relative_to(ROOT).as_posix(), path.read_text(encoding="utf-8"))
    return out


def test_every_call_in_the_contract_is_in_its_source_file():
    gone = []
    for call in calls():
        text = (ROOT / call["source"]).read_text(encoding="utf-8")
        gone += [f"{key(call)}: {needle}" for needle in call["needles"] if needle not in text]
    assert gone == []


def test_every_version_a_call_takes_from_a_setting_is_its_default():
    # A path built from a setting or constant is listed with its default version; changing the default must
    # change the contract (and the snapshot) too
    wrong = []
    for call in calls():
        version = VERSION.match(call["path"])[1]
        if source := call.get("version_from"):
            needle = source["needle"]
            if needle not in (ROOT / source["source"]).read_text(encoding="utf-8"):
                wrong.append(f"{key(call)}: {needle} is not in {source['source']}")
            elif f'"{version}"' not in needle:
                wrong.append(f"{key(call)}: {needle} does not set {version}")
        elif not any(version in needle for needle in call["needles"]):
            wrong.append(f"{key(call)}: no needle names {version}; add version_from")
    assert wrong == []


def test_every_obp_api_call_in_the_source_is_in_the_contract():
    # A line that makes an OBP-API call must contain a needle of a call listed for its file, so a new call cannot
    # reach OBP-API without its fields being recorded
    listed = calls()
    unlisted = [
        f"{file}:{n} {line.strip()}" for file, n, line in obp_path_lines()
        if not any(c["source"] == file and any(needle.strip() in line for needle in c["needles"]) for c in listed)
        and not any(f == file and fragment in line for f, fragment in NOT_CALLS)
    ]
    assert unlisted == []


def test_every_line_listed_as_not_a_call_still_exists():
    stale = [f"{f}|{fragment}" for f, fragment in NOT_CALLS if fragment not in (ROOT / f).read_text(encoding="utf-8")]
    assert stale == []


def test_finds_the_calls_in_the_source():
    # So an empty coverage result means "every call listed", not "no lines found"
    files = {file for file, _, _ in obp_path_lines()}
    assert {"src/obp_sentinel/obp_client.py", "src/obp_sentinel/config.py", "src/obp_sentinel/web.py",
            "src/obp_sentinel/collector.py"} <= files
    assert len(obp_path_lines()) >= 15


def test_the_snapshot_was_taken_for_this_contract():
    # Otherwise rerun snapshot.py
    assert sorted(map(key, calls())) == sorted(map(key, load("snapshot.json")["endpoints"]))


def test_every_call_reaches_an_endpoint_obp_api_documents():
    # Unless the entry records why not (undocumented_endpoint). A recorded gap OBP-API has since documented fails
    # too, so the note goes
    snapshot = load("snapshot.json")
    problems = []
    for call in calls():
        entry = documented(call, snapshot) or {"missing": True}
        missing, known_gap = entry.get("missing", False), call.get("undocumented_endpoint")
        if missing and not known_gap:
            problems.append(f"{key(call)}: no documented endpoint")
        elif not missing and known_gap:
            problems.append(f"{key(call)}: now documented as {entry['operation_id']}; drop undocumented_endpoint")
        if entry.get("ambiguous"):
            problems.append(f"{key(call)}: matches several endpoints {entry['ambiguous']}")
    assert problems == []


def test_every_field_the_code_reads_is_documented():
    # Unless the entry lists it in undocumented_fields; a listed field that OBP-API now documents fails, so the
    # list shrinks
    snapshot = load("snapshot.json")
    problems = []
    for call in calls():
        entry = documented(call, snapshot)
        if not entry or entry.get("missing"):
            continue
        fields = set(entry["fields"])
        known = set(call.get("undocumented_fields", []))
        problems += [f"{key(call)} ({entry['operation_id']}): reads {read}, not documented"
                     for read in undocumented_reads(call, fields) if read not in known]
        problems += [f"{key(call)}: {field} is now documented; drop it from undocumented_fields"
                     for field in sorted(known & fields)]
    assert problems == []


def test_a_renamed_or_removed_field_is_reported():
    # With git_commit gone from the documented fields, the root read is reported, and a read with an alternative
    # passes on either field
    root = {"reads": ["git_commit"]}
    assert undocumented_reads(root, {"version", "hostname"}) == ["git_commit"]
    either = {"reads": ["allowed|allowed_for_auth_modes"]}
    assert undocumented_reads(either, {"allowed_for_auth_modes"}) == []
    assert undocumented_reads(either, set()) == ["allowed|allowed_for_auth_modes"]


def test_the_scan_finds_a_path_put_together_from_pieces_a_path_without_a_version_and_a_request_by_url():
    # The scan itself: base + "/obp/" + version, a /obp/dynamic-entity path, a _request_url call (also when its URL is
    # set on a line above) and a plain _request call are found; /obp-oidc, a docstring and a comment are not
    source = '''def probe(client, base, ver, url):
    """Docstring naming /obp/v7.0.0/root."""
    a = base + "/obp/" + ver + "/banks"
    b = f"{base}/obp/dynamic-entity/x"
    c = "https://host/obp/dynamic-entity/y"
    d = "http://localhost:9000/obp-oidc"  # /obp/v7.0.0/root
    url = base + ver
    e = client._request_url("GET", url)
    f = client._request("GET", ver, "banks")
    g = other.fetch(url)
    return a, b, c, d, e, f, g
'''
    found = obp_path_lines_in("example.py", source)
    assert [n for _, n, _ in found] == [3, 4, 5, 8, 9]
    assert "url = base + ver" in found[3][2]
