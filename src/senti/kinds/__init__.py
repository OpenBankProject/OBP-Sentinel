"""Kinds: what a service's code is. A kind says how its log lines read (signatures.py), what every AI run is told
about that kind of code (its notes, context.py), what to look for when tracing where a problem came from (history.py),
and how a fix is made and checked (its toolchain, fixer.py). What Senti learns from the person's responses is kept per
kind (preferences.py).

Each general kind is a module here; a pack adds its own (packs/obp: obp-api). The general kinds take the OBP log
format for now, which the OBP apps of those kinds need for their signature ids to stay the same.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

NOTES = Path(__file__).parent / "notes"


@dataclass(frozen=True)
class LogFormat:
    """How to read a kind's log lines (signatures.py)."""

    line: re.Pattern  # groups ts, thread, logger and body; a line that does not match is a plain message
    exceptions: tuple[re.Pattern, ...]  # group 1 is an exception's name; each is tried in turn, on detail then message
    path: re.Pattern  # a request path in a line: kept as the endpoint, and generalised in the template
    path_kept: int  # how many leading path segments are kept as they are: ['', 'obp', 'v5.1.0'] is 3


@dataclass(frozen=True)
class Toolchain:
    """How a fix is made and checked in this kind of code (fixer.py). Paths are relative to the worktree, Senti's own
    git worktree of the checkout."""

    build: Callable[[str], list[str]]  # the build command for a worktree, without its goal
    compile_goal: tuple[str, ...]  # added to `build`: compiles the main and the test code
    test_goal: str  # added to `build` in the prompt: runs only the named tests
    source_dirs: tuple[str, ...]  # what a fix may change
    scripts: tuple[str, ...] = ()  # scripts in the worktree a fix may run with python3
    copied: tuple[str, ...] = ()  # git-ignored files the build needs, copied from the checkout (glob patterns)
    fix_notes: str = ""  # rules for a fix in this kind of code, for the prompt; {worktree} is filled in


@dataclass(frozen=True)
class Kind:
    name: str
    description: str
    log_format: LogFormat
    notes: str = ""  # what every AI run is told about this kind of code (Markdown)
    history_notes: str = ""  # what to know when tracing which commit introduced a problem
    toolchain: Toolchain | None = None  # None: Fix this is not set up for this kind yet


def notes(path: Path) -> str:
    """A notes file's text, without its trailing newline."""
    return path.read_text().strip()


def _all() -> dict[str, Kind]:
    from ..packs import obp
    from . import scala_http4s, sveltekit, vue_express

    return {k.name: k for k in (*obp.kinds(), sveltekit.KIND, vue_express.KIND, scala_http4s.KIND)}


KINDS = _all()


def kind_of(name: str) -> Kind:
    try:
        return KINDS[name]
    except KeyError:
        raise ValueError(f"Senti does not know the kind {name!r} ({', '.join(KINDS)})") from None
