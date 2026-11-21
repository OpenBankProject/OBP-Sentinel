"""A Scala service on http4s and cats-effect, built with sbt.

OBP-OIDC writes `logger - message` or plain messages, an exception followed by its `\tat ...` stack lines.
"""

from . import NOTES, Kind, notes
from ..packs.obp.kind import OBP_LOG_FORMAT  # until a general format exists: keeps the OBP apps' signature ids

KIND = Kind("scala-http4s", __doc__.split("\n")[0], OBP_LOG_FORMAT, notes(NOTES / "scala-http4s.md"))
