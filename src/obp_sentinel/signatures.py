"""Turning raw log lines into signatures: one stable id per distinct problem.

OBP-API writes log cache entries as `[timestamp] [thread] [logger] message`, where the message
may be followed by a line holding the exception (`Throwable.toString`). Two lines that differ only
in ids, numbers, amounts or quoted values belong to the same signature.
"""

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime

LINE_RE = re.compile(r"^\[(?P<ts>[^\]]+)\]\s+\[(?P<thread>[^\]]*)\]\s+\[(?P<logger>[^\]]*)\]\s?(?P<body>.*)$", re.S)
EXCEPTION_RE = re.compile(r"\b((?:[a-zA-Z_$][\w$]*\.)+[A-Z][\w$]*(?:Exception|Error|Throwable|Failure)[\w$]*)")
OBP_PATH_RE = re.compile(r"/obp/v\d+\.\d+\.\d+(?:/[^\s\"'?,;)\]]*)?")
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


def parse_line(raw: str) -> ParsedLine:
    match = LINE_RE.match(raw)
    if not match:
        first, _, rest = raw.partition("\n")
        return ParsedLine(None, "", "", first, rest)
    first, _, rest = match["body"].partition("\n")
    return ParsedLine(_parse_ts(match["ts"]), match["thread"], match["logger"], first, rest)


def _parse_ts(value: str) -> datetime | None:
    # OBP-API uses SimpleDateFormat("yyyy-MM-dd HH:mm:ssX"), e.g. 2026-10-04 09:12:01+02 or ...Z
    value = value.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    elif re.search(r"[+-]\d{2}$", value):
        value += ":00"
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def normalise_path(path: str) -> str:
    segments = path.split("/")
    # ['', 'obp', 'v5.1.0', ...]: keep the version, generalise anything that does not look like a literal
    kept = segments[:3] + ["{id}" if s and not LITERAL_SEGMENT_RE.match(s) else s for s in segments[3:]]
    return "/".join(kept)


def normalise_text(text: str) -> str:
    text = OBP_PATH_RE.sub(lambda m: normalise_path(m.group(0)), text)
    for pattern, replacement in NORMALISERS:
        text = pattern.sub(replacement, text)
    return re.sub(r"\s+", " ", text).strip()[:TEMPLATE_MAX]


def find_exception(parsed: ParsedLine) -> str:
    for text in (parsed.detail, parsed.message):
        match = EXCEPTION_RE.search(text)
        if match:
            return match.group(1)
    return ""


def find_endpoint(parsed: ParsedLine) -> str:
    match = OBP_PATH_RE.search(parsed.message) or OBP_PATH_RE.search(parsed.detail)
    return normalise_path(match.group(0)) if match else ""


def signature_of(level: str, parsed: ParsedLine) -> Signature:
    exception = find_exception(parsed)
    template = normalise_text(parsed.message)
    key = "|".join([level.lower(), parsed.logger, exception, template])
    return Signature(
        id=hashlib.sha1(key.encode()).hexdigest()[:12],
        level=level.lower(),
        logger=parsed.logger,
        exception=exception,
        endpoint=find_endpoint(parsed),
        template=template,
    )
