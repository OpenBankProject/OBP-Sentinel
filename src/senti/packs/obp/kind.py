"""OBP-API: Scala, http4s with Lift history, resource docs, Roles and connectors, built with Maven.

OBP-API writes log cache entries as `[timestamp] [thread] [logger] message`, where the message may be followed by a
line holding the exception (`Throwable.toString`).

OBP_LOG_FORMAT is shared by the OBP apps' kinds: their lines mention OBP-API paths, and a signature's id depends on how
a line is read, so all OBP services read lines the same way and their history stays comparable.
"""

import re

from ...kinds import Kind, LogFormat

OBP_LOG_FORMAT = LogFormat(
    line=re.compile(r"^\[(?P<ts>[^\]]+)\]\s+\[(?P<thread>[^\]]*)\]\s+\[(?P<logger>[^\]]*)\]\s?(?P<body>.*)$", re.S),
    exceptions=(
        # A JVM exception: java.sql.SQLTransientConnectionException, net.liftweb...Failure
        re.compile(r"\b((?:[a-zA-Z_$][\w$]*\.)+[A-Z][\w$]*(?:Exception|Error|Throwable|Failure)[\w$]*)"),
        # A JavaScript error as Node prints it: `TypeError: ...`, `Error: ...`, `OBPAPIError: ...`
        re.compile(r"\b([A-Z][\w$]*Error|Error)(?=: )"),
    ),
    path=re.compile(r"/obp/v\d+\.\d+\.\d+(?:/[^\s\"'?,;)\]]*)?"),
    path_kept=3,
)

KIND = Kind("obp-api", "OBP-API: Scala, http4s with Lift history, resource docs, Roles and connectors, built with Maven.",
            OBP_LOG_FORMAT)
