"""Data sources: where Senti reads an instance's logs, Telemetry, metrics and running commit from.

A source offers what it can, named in `offers`, and the collector polls only that. It records what comes back; it
knows nothing about where it came from. The general sources, any company's to use, are modules here: Prometheus and
Loki come next (docs/generic-services-plan.md, phase 5). A pack adds its own: packs/obp has `obp` (OBP-API's own
endpoints) and `obp-app` (an OBP app's /monitoring endpoints).
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

    return {**obp.sources()}


def source_for(config: "Config", http=None) -> Source:
    """The source an instance is read from (config.source). `http`: an httpx.Client to send its requests with (tests)."""
    return sources()[config.source](config, http)
