"""Kinds: what a service's code is. A kind says how its log lines read, and later what the analyst is told about it
and how a fix is built and tested (docs/generic-services-plan.md). What Senti learns from the person's responses is
kept per kind (preferences.py).

Each general kind is a module here; a pack adds its own (packs/obp: obp-api). The general kinds take the OBP log
format for now, which the OBP apps of those kinds need for their signature ids to stay the same.
"""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class LogFormat:
    """How to read a kind's log lines (signatures.py)."""

    line: re.Pattern  # groups ts, thread, logger and body; a line that does not match is a plain message
    exceptions: tuple[re.Pattern, ...]  # group 1 is an exception's name; each is tried in turn, on detail then message
    path: re.Pattern  # a request path in a line: kept as the endpoint, and generalised in the template
    path_kept: int  # how many leading path segments are kept as they are: ['', 'obp', 'v5.1.0'] is 3


@dataclass(frozen=True)
class Kind:
    name: str
    description: str
    log_format: LogFormat


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
