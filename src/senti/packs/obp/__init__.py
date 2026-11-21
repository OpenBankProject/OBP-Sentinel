"""The OBP pack: what Senti needs to watch the Open Bank Project.

- api_source.py: the `obp` source, OBP-API read as a Platform App (log cache, Telemetry, aggregate metrics, the root
  endpoint, resource docs and Roles, and the Scope declaration).
- app_source.py: the `obp-app` source, an OBP app's /monitoring endpoints, in OBP-API's shape.
- app_directory.py: the OBP apps, found in an OBP-API instance's app directory.
- kind.py: the `obp-api` kind, and the log format the OBP services share.
- toolchain.py: how Fix this makes and checks a fix in OBP-API (Maven, the parity scripts).
- notes/: what every AI run is told about OBP-API code (obp-api.md), and about an OBP app (obp-app.md).
- lift-reference.md: per OBP-API version, the last commit at which its Lift code was live (before http4s).

Still outside the pack (docs/generic-services-plan.md): the Telemetry sections of the summary (phase 5), the source
review, which only OBP-API offers so far, and the OBP wording of the health page.
"""

from pathlib import Path

LIFT_REFERENCE = Path(__file__).parent / "lift-reference.md"
APP_NOTES = (Path(__file__).parent / "notes" / "obp-app.md").read_text().strip()  # every OBP app's service notes


def sources() -> dict:
    """The pack's data sources, by name."""
    from .api_source import ObpSource
    from .app_source import ObpAppSource

    return {ObpSource.name: ObpSource, ObpAppSource.name: ObpAppSource}


def kinds() -> list:
    from .kind import KIND

    return [KIND]
