"""The OBP-API version every Sentinel call uses.

OBP-API deployments can switch off the versions below v5.1.0, so every path Sentinel sends to OBP-API must use
v5.1.0 or newer. Sentinel builds some paths from a version setting (OBP_ROOT_API_VERSION ...), so this test reads
the sources and the example settings and fails on any `obp/vX.Y.Z` below v5.1.0, and on any version written on its
own as a value (`"v4.0.0"`, `=v4.0.0`), that is not listed in BELOW_V510. So a new call on an old version cannot
slip in.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# (file relative to the repository, a fragment of the line) -> why that line still names a version below v5.1.0.
# Empty: every call has a v5.1.0 or newer equivalent.
BELOW_V510: dict[tuple[str, str], str] = {}
SOURCE_DIRS = ["src"]
SOURCE_FILES = [".env.example"]
EXTENSIONS = {".py", ".html", ".js"}
SKIP_DIRS = {"__pycache__", "node_modules", ".venv"}
# A version in a path (`obp/v4.0.0`) or written on its own as a value (`"v4.0.0"`, `'v4.0.0'`, `=v4.0.0`)
VERSION = re.compile(r"(?:obp/|[\"'=])v(\d+)\.(\d+)\.(\d+)(?![\w.])")


def versions_below_v510(line: str) -> list[str]:
    """Every version below v5.1.0 that `line` uses, as `vX.Y.Z`."""
    return [f"v{m[1]}.{m[2]}.{m[3]}" for m in VERSION.finditer(line)
            if (int(m[1]), int(m[2]), int(m[3])) < (5, 1, 0)]


def source_files() -> list[Path]:
    files = [ROOT / f for f in SOURCE_FILES if (ROOT / f).is_file()]
    for d in SOURCE_DIRS:
        files += [p for p in (ROOT / d).rglob("*")
                  if p.suffix in EXTENSIONS and p.is_file() and not SKIP_DIRS & set(p.relative_to(ROOT).parts)]
    return sorted(files)


def calls_below_v510() -> list[str]:
    found = []
    for path in source_files():
        rel = path.relative_to(ROOT).as_posix()
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for version in versions_below_v510(line):
                if not any(f == rel and fragment in line for f, fragment in BELOW_V510):
                    found.append(f"{rel}:{n} {version}")
    return found


def test_finds_the_sources():
    # So an empty result means "no old version", not "no files"
    assert len(source_files()) > 20


def test_calls_obp_api_on_v510_or_newer():
    assert calls_below_v510() == []


def test_every_listed_line_still_exists():
    # A call that moved to v5.1.0 must also leave BELOW_V510
    stale = [f"{f}|{fragment}" for f, fragment in BELOW_V510
             if fragment not in (ROOT / f).read_text(encoding="utf-8")]
    assert stale == []


def test_recognises_the_ways_an_old_version_can_be_written():
    assert versions_below_v510('url = f"{base}/obp/v4.0.0/x"') == ["v4.0.0"]
    assert versions_below_v510("GET obp/v5.0.0") == ["v5.0.0"]
    assert versions_below_v510('self._request("GET", "v4.0.0", "api/versions")') == ["v4.0.0"]
    assert versions_below_v510('env("OBP_ROOT_API_VERSION", \'v3.1.0\')') == ["v3.1.0"]
    assert versions_below_v510("OBP_ROOT_API_VERSION=v2.2.0") == ["v2.2.0"]
    assert versions_below_v510('"/obp/v1.2.1/a" and "/obp/v4.0.0/b"') == ["v1.2.1", "v4.0.0"]
    assert versions_below_v510('url = f"{base}/obp/v5.1.0/x"') == []
    assert versions_below_v510('"/obp/v6.0.0/root" "v7.0.0" OBP_X=v5.1.0') == []
    assert versions_below_v510('f"{base}/obp/dynamic-entity/x"') == []
    # A fully qualified version names the docs to read, not the version the call is made on
    assert versions_below_v510('resource_docs("OBPv4.0.0")') == []
