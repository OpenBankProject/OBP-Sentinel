"""Turning raw log lines into signatures: one stable id per distinct problem.

Two lines that differ only in ids, numbers, amounts or quoted values belong to the same signature. How a line is laid
out, how an exception is named and what a request path looks like come from the service's kind (kinds/).
"""

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime

from .kinds import Kind

LITERAL_SEGMENT_RE = re.compile(r"^[a-z]+(?:-[a-z]+)*$")

TEMPLATE_MAX = 300

# Order matters: the specific shapes go before the general ones.
NORMALISERS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "<uuid>"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "<email>"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?"), "<ts>"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"), "<ip>"),
    (re.compile(r"'[^'\n]{0,200}'"), "'<s>'"),
    (re.compile(r'"[^"\n]{0,200}"'), '"<s>"'),
    (re.compile(r"\b[0-9a-fA-F]{16,}\b"), "<hex>"),
    (re.compile(r"\b[A-Za-z0-9_-]*\d[A-Za-z0-9_-]*[A-Za-z][A-Za-z0-9_-]{18,}\b"), "<token>"),
    (re.compile(r"(?<![\w<])-?\d+(?:\.\d+)?(?![\w>])"), "<n>"),
]


@dataclass(frozen=True)
class Signature:
    id: str
    level: str
    logger: str
    exception: str
    endpoint: str
    template: str


@dataclass(frozen=True)
class ParsedLine:
    ts: datetime | None
    thread: str
    logger: str
    message: str
    detail: str


def parse_line(raw: str, kind: Kind) -> ParsedLine:
    match = kind.log_format.line.match(raw)
    if not match:
        first, _, rest = raw.partition("\n")
        return ParsedLine(None, "", "", first, rest)
    first, _, rest = match["body"].partition("\n")
    return ParsedLine(_parse_ts(match["ts"]), match["thread"], match["logger"], first, rest)


def _parse_ts(value: str) -> datetime | None:
    # ISO 8601, or OBP-API's SimpleDateFormat("yyyy-MM-dd HH:mm:ssX"), e.g. 2026-10-04 09:12:01+02 or ...Z
    value = value.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    elif re.search(r"[+-]\d{2}$", value):
        value += ":00"
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def normalise_path(path: str, kind: Kind) -> str:
    segments = path.split("/")
    # ['', 'obp', 'v5.1.0', ...]: keep the start (OBP: the version), generalise anything that does not look like a literal
    keep = kind.log_format.path_kept
    kept = segments[:keep] + ["{id}" if s and not LITERAL_SEGMENT_RE.match(s) else s for s in segments[keep:]]
    return "/".join(kept)


def normalise_text(text: str, kind: Kind) -> str:
    text = kind.log_format.path.sub(lambda m: normalise_path(m.group(0), kind), text)
    for pattern, replacement in NORMALISERS:
        text = pattern.sub(replacement, text)
    return re.sub(r"\s+", " ", text).strip()[:TEMPLATE_MAX]


def find_exception(parsed: ParsedLine, kind: Kind) -> str:
    for pattern in kind.log_format.exceptions:
        for text in (parsed.detail, parsed.message):
            match = pattern.search(text)
            if match:
                return match.group(1)
    return ""


def find_endpoint(parsed: ParsedLine, kind: Kind) -> str:
    path = kind.log_format.path
    match = path.search(parsed.message) or path.search(parsed.detail)
    return normalise_path(match.group(0), kind) if match else ""


def signature_of(level: str, parsed: ParsedLine, kind: Kind) -> Signature:
    exception = find_exception(parsed, kind)
    template = normalise_text(parsed.message, kind)
    key = "|".join([level.lower(), parsed.logger, exception, template])
    return Signature(
        id=hashlib.sha1(key.encode()).hexdigest()[:12],
        level=level.lower(),
        logger=parsed.logger,
        exception=exception,
        endpoint=find_endpoint(parsed, kind),
        template=template,
    )
