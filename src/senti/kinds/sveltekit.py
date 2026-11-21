"""A SvelteKit app on Node: server routes, sessions, calls to an API.

The OBP apps of this kind (the API Manager, the Portal, Cohort, the Sandbox Populator) write
`[timestamp] [LEVEL] [context] message`; an error is `TypeError: message` followed by `    at ...` stack lines.
"""

from . import NOTES, Kind, notes
from ..packs.obp.kind import OBP_LOG_FORMAT  # until a general format exists: keeps the OBP apps' signature ids

KIND = Kind("sveltekit", __doc__.split("\n")[0], OBP_LOG_FORMAT, notes(NOTES / "sveltekit.md"))
