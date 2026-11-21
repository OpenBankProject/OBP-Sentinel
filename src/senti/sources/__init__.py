"""Data sources: where Senti reads an instance's logs, Telemetry, metrics and running commit from.

A source offers what it can, named in `offers`, and the collector polls only that. It records what comes back; it
knows nothing about where it came from. The general sources, any company's to use, are modules here: `prometheus`
(Telemetry, metrics and the running commit, from Prometheus's query API) and `loki` (log lines, from Loki's). A pack
adds its own: packs/obp has `obp` (OBP-API's own endpoints) and `obp-app` (an OBP app's /monitoring endpoints).

An instance can read several sources (its service's `sources` in senti.toml): each kind of data comes from the first
one that offers it, so ["obp", "prometheus"] takes OBP-API's own Telemetry and only what obp does not offer from
Prometheus.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import Config
    from ..store import Store

LOGS = "logs"  # log lines, newest first
TELEMETRY = "telemetry"  # meters and their values now
METRICS = "metrics"  # calls and response times per time bucket
COMMIT = "commit"  # the commit the instance runs, read on its own (else from the Telemetry, if it says)
REACH = "reach"  # the endpoints served and who can reach them, for the source review


class Source:
    """What every source offers. A method a source does not offer is never called."""

    name = ""
    offers: frozenset[str] = frozenset()

    def __init__(self, config: "Config"):
        self.config = config

    def logs(self, level: str, limit: int) -> list[str]:
        """Raw log lines of one level, newest first."""
        raise NotImplementedError(f"the {self.name} source has no logs")

    def telemetry(self) -> dict:
        """{"meters": [{"name", "type", "tags", "measurements"}], "git_commit"?, "api_instance_id"?}."""
        raise NotImplementedError(f"the {self.name} source has no Telemetry")

    def metrics(self, from_ts: int, to_ts: int) -> dict:
        """Calls in [from_ts, to_ts): count, average and maximum response time, distinct consumers and users."""
        raise NotImplementedError(f"the {self.name} source has no metrics")

    def running_commit(self) -> str | None:
        raise NotImplementedError(f"the {self.name} source cannot say which commit runs")

    def reach(self, git_commit: str | None) -> dict:
        """The endpoints the instance serves, with what each needs, and the Roles anyone holds (source.py)."""
        raise NotImplementedError(f"the {self.name} source cannot list endpoints")

    def secured_paths(self) -> list[str]:
        """The paths Senti reads with its token, which must refuse a call without one."""
        return []

    def status_without_token(self, url: str) -> int | None:
        """The HTTP status of a GET sent with no token, or None if there was no answer."""
        return None

    def before_poll(self, store: "Store") -> None:
        """Anything to do before each poll, such as telling the system what Senti needs to read."""

    def after_poll(self, store: "Store") -> str | None:
        """Anything to do after each poll, such as finding out why a read was refused. A few words for the log."""
        return None


def sources() -> dict[str, type[Source]]:
    """Every source, by name: the general ones and the packs'."""
    from ..packs import obp
    from .loki import LokiSource
    from .prometheus import PrometheusSource

    return {PrometheusSource.name: PrometheusSource, LokiSource.name: LokiSource, **obp.sources()}


class Combined(Source):
    """Several sources read as one: each kind of data from the first that offers it."""

    def __init__(self, config: "Config", parts: list[Source]):
        super().__init__(config)
        self.parts = parts
        self.name = "+".join(p.name for p in parts)
        self.offers = frozenset().union(*(p.offers for p in parts))

    def _for(self, what: str) -> Source:
        return next(p for p in self.parts if what in p.offers)

    def logs(self, level: str, limit: int) -> list[str]:
        return self._for(LOGS).logs(level, limit)

    def telemetry(self) -> dict:
        return self._for(TELEMETRY).telemetry()

    def metrics(self, from_ts: int, to_ts: int) -> dict:
        return self._for(METRICS).metrics(from_ts, to_ts)

    def running_commit(self) -> str | None:
        return self._for(COMMIT).running_commit()

    def reach(self, git_commit: str | None) -> dict:
        return self._for(REACH).reach(git_commit)

    def _secured(self) -> Source | None:
        return next((p for p in self.parts if p.secured_paths()), None)

    def secured_paths(self) -> list[str]:
        return part.secured_paths() if (part := self._secured()) else []

    def status_without_token(self, url: str) -> int | None:
        return part.status_without_token(url) if (part := self._secured()) else None

    def before_poll(self, store: "Store") -> None:
        for p in self.parts:
            p.before_poll(store)

    def after_poll(self, store: "Store") -> str | None:
        said = [x for p in self.parts if (x := p.after_poll(store))]
        return ", ".join(said) or None


def offers_of(config: "Config") -> frozenset[str]:
    """What an instance's sources offer between them, without making a client."""
    known = sources()
    return frozenset().union(*(known[name].offers for name in config.read_from if name in known))


def source_for(config: "Config", http=None) -> Source:
    """The source an instance is read from (config.read_from). `http`: an httpx.Client to send its requests with
    (tests)."""
    known = sources()
    if unknown := [name for name in config.read_from if name not in known]:
        raise ValueError(f"{config.name} reads {', '.join(unknown)}, which Senti does not know ({', '.join(known)})")
    parts = [known[name](config, http) for name in config.read_from]
    return parts[0] if len(parts) == 1 else Combined(config, parts)
