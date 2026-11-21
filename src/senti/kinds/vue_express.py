"""A Vue single-page app with an Express server.

API Explorer II writes plain messages; an error is `TypeError: message` followed by `    at ...` stack lines.
"""

from . import Kind
from ..packs.obp.kind import OBP_LOG_FORMAT  # until a general format exists: keeps the OBP apps' signature ids

KIND = Kind("vue-express", __doc__.split("\n")[0], OBP_LOG_FORMAT)
