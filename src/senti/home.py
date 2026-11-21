"""Senti's own folder: where its settings (.env, senti.toml), databases, digests, source review record and work files
are. Every relative path Senti uses is resolved against it, so a `senti` command run from another folder reads and
writes Senti's files, never new ones there. (Claude, reading a service's code, once ran `senti digest` from that
service's checkout, and Senti made empty databases there.)

The folder is SENTI_HOME when that is set; else the nearest folder, from the working directory upwards, that holds
Senti (senti.toml, or Senti's agent files); else the working directory. `senti` sets SENTI_HOME when it starts, so
every process it starts (the analyst, the source review, Fix this, and the `senti` commands they run) inherits it.
"""

import os
from pathlib import Path

HOME = "SENTI_HOME"
MARKS = ("senti.toml", ".claude/agents/senti-analyst.md")


def found() -> Path | None:
    """SENTI_HOME, else the nearest folder from here upwards that holds Senti; None if there is none."""
    if os.environ.get(HOME):
        return Path(os.environ[HOME]).resolve()
    here = Path.cwd().resolve()
    return next((folder for folder in (here, *here.parents) if any((folder / mark).exists() for mark in MARKS)), None)


def home() -> Path:
    return found() or Path.cwd().resolve()


def in_home(path: str) -> str:
    """`path` as it is if absolute, else in Senti's folder. Empty stays empty (a setting that is not set)."""
    return str(home() / path) if path and not Path(path).is_absolute() else path


def remember() -> str | None:
    """Fix Senti's folder for this process and every process it starts. Why it cannot, if it cannot: run outside
    Senti's folder without SENTI_HOME, it would otherwise make new, empty files where it runs."""
    folder = found()
    if not folder:
        return (f"{Path.cwd()} is not in Senti's folder (the one with senti.toml or .claude/agents/senti-analyst.md): "
                "run senti from there, or set SENTI_HOME to it")
    os.environ[HOME] = str(folder)
    return None
