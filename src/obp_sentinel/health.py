"""What stops Sentinel from watching an instance, and what the operator should do about it.

Built from each source's latest poll and the collector's last root call and Platform App declaration, all in the
store: no calls to OBP-API. Failures with the same cause are merged into one problem (a rejected token fails every
source), so the operator sees each thing to fix once.
"""

import re

from .config import Config
from .store import Store, now

SOURCE_NAMES = {"log:error": "Error log", "log:warning": "Warning log", "log:info": "Info log",
                "log:debug": "Debug log", "telemetry": "Telemetry", "metrics": "Aggregate metrics",
                "root": "Root endpoint", "platform-app": "Scope declaration"}
API_VERSION_SETTINGS = {"telemetry": "OBP_TELEMETRY_API_VERSION", "metrics": "OBP_AGGREGATE_METRICS_API_VERSION",
                        "root": "OBP_ROOT_API_VERSION"}
HTTP_FAILURE = re.compile(r"^(?P<method>[A-Z]+) (?P<url>\S+) failed \((?P<status>\d+)\): (?P<body>.*)$", re.S)
OBP_VERSION_PATH = re.compile(r"/obp/(?P<version>v\d+\.\d+\.\d+)/(?P<path>[^?\s]+)")
ROLE = re.compile(r"\bCan[A-Z]\w+")
ORDER = ["settings", "oidc-discovery", "oidc-token", "unreachable", "token-rejected", "not-platform-app",
         "missing-scopes", "not-served", "server-error", "other"]


def _source_name(source: str) -> str:
    return SOURCE_NAMES.get(source, source)


def diagnose(source: str, error: str, config: Config) -> dict:
    """The kind of problem behind one failure, and what to do about it."""
    p = config.env_prefix
    if "must be set" in error:
        return {"key": "settings", "title": "Sentinel's OIDC settings are missing",
                "action": f"Set {p}OIDC_ISSUER, {p}OIDC_CLIENT_ID and {p}OIDC_CLIENT_SECRET (in .env), then restart Sentinel."}
    if error.startswith("OIDC discovery"):
        return {"key": "oidc-discovery", "title": "Sentinel cannot find the OIDC provider",
                "action": f"Check {p}OIDC_ISSUER ({config.oidc_issuer}): {config.oidc_issuer}/.well-known/openid-configuration "
                          "should answer with the provider's settings."}
    if error.startswith("Client credentials token request"):
        return {"key": "oidc-token", "title": "The OIDC provider refuses to give Sentinel a token",
                "action": f"Check {p}OIDC_CLIENT_ID ({config.oidc_client_id}) and {p}OIDC_CLIENT_SECRET, and that this "
                          "client is registered at the provider and allowed the client_credentials grant."}
    m = HTTP_FAILURE.match(error)
    if not m:
        if " failed: " in error:  # httpx could not connect, or timed out
            return {"key": "unreachable", "title": "Sentinel cannot reach OBP-API or the OIDC provider",
                    "action": f"Check {p}OBP_BASE_URL ({config.obp_base_url}) and {p}OIDC_ISSUER ({config.oidc_issuer}), "
                              "and that this machine can reach them."}
        return {"key": "other", "title": f"{_source_name(source)} fails", "action": "See what Sentinel got below."}
    status, body = int(m["status"]), m["body"]
    if status == 401 or "OBP-20200" in body or "OBP-20206" in body:
        return {"key": "token-rejected", "title": "OBP-API does not accept Sentinel's token",
                "action": "Sentinel gets a token from the OIDC provider, but OBP-API cannot verify it. In OBP-API's props, "
                          "add the provider's JWKS URL (the jwks_uri listed at "
                          f"{config.oidc_issuer}/.well-known/openid-configuration) to oauth2.jwk_set.url, check the "
                          "issuer OBP-API expects, and restart OBP-API."}
    if status == 403 and "OBP-20006" in body:
        roles = ROLE.findall(body)
        return {"key": "missing-scopes", "title": "Sentinel lacks Scopes", "roles": [roles] if roles else []}
    if status == 404 and "OBP-10404" in body:
        where = OBP_VERSION_PATH.search(m["url"])
        version, path = (where["version"], where["path"]) if where else ("?", m["url"])
        if source == "platform-app":
            return {"key": "not-served", "title": f"This OBP-API has no {version}, so Sentinel cannot declare its Scopes",
                    "action": "Upgrade OBP-API to have Sentinel's Scopes listed for an administrator. Until then, "
                              "an administrator must grant them by hand.", "warn": True}
        setting = API_VERSION_SETTINGS.get(source) or ("OBP_LOG_CACHE_API_VERSION" if source.startswith("log:") else None)
        return {"key": "not-served", "title": f"This OBP-API does not serve {version}/{path}",
                "action": "It is probably older than that version. Upgrade OBP-API"
                          + (f", or set {p}{setting} to a version it serves." if setting else ".")}
    if source == "platform-app":
        return {"key": "not-platform-app", "title": "Sentinel's Consumer is not marked as a Platform App",
                "action": f"Ask an OBP-API administrator to mark the Consumer of OIDC client {config.oidc_client_id} as a "
                          "Platform App. It takes effect without a restart."}
    if status >= 500:
        return {"key": "server-error", "title": f"OBP-API fails ({status}) on {_source_name(source)}",
                "action": "OBP-API has an internal problem here: look in its own logs."}
    return {"key": "other", "title": f"{_source_name(source)} fails ({status})", "action": "See what Sentinel got below."}


def health(store: Store, config: Config) -> dict:
    """The problems to fix, most blocking first. None when everything Sentinel polls works."""
    latest = [dict(r) for r in store.db.execute(
        """SELECT p.source, p.ts, p.ok, p.error FROM polls p
           JOIN (SELECT source, MAX(id) AS id FROM polls WHERE ts > ? GROUP BY source) l ON l.id = p.id""",
        (now() - 86400,),
    )]
    for key, source in (("root", "root"), ("platform_app", "platform-app")):
        if (s := store.get_state(key)) is not None:
            latest.append({"source": source, **s})
    app = store.get_state("platform_app") or {}
    who = f"Consumer {app['consumer_id']}" if app.get("consumer_id") else f"the Consumer of OIDC client {config.oidc_client_id}"

    problems: dict[str, dict] = {}
    missing_roles: list[list[str]] = [[r] for r in app.get("missing") or []]
    for s in sorted(latest, key=lambda s: s["source"]):
        if s["ok"] or not s.get("error"):
            continue
        d = diagnose(s["source"], s["error"], config)
        missing_roles += d.pop("roles", [])
        severity = "warn" if d.pop("warn", False) else "bad"
        key = d["key"] + (":" + d["title"] if d["key"] in ("not-served", "other", "server-error") else "")
        problem = problems.setdefault(key, {**d, "severity": severity, "sources": [], "error": s["error"], "since": s["ts"]})
        problem["sources"].append(_source_name(s["source"]))

    if missing_roles:
        unique = list({tuple(r): r for r in missing_roles}.values())
        names = ", ".join(r[0] + (f" (or {' or '.join(r[1:])})" if len(r) > 1 else "") for r in unique)
        problem = problems.setdefault("missing-scopes", {
            "key": "missing-scopes", "title": "Sentinel lacks Scopes", "severity": "bad", "sources": [],
            "error": None, "since": app.get("ts"),
        })
        problem["action"] = f"Ask an OBP-API administrator to grant {who} these Scopes: {names}."
        if not problem["sources"]:
            problem["sources"] = [_source_name("platform-app")]

    ordered = sorted(problems.values(), key=lambda p: (ORDER.index(p["key"]), p["title"]))
    blocking = {"settings", "oidc-discovery", "oidc-token", "unreachable", "token-rejected"}
    return {
        "problems": ordered,
        # Behind a rejected token, OBP-API cannot say which Scopes are missing: say so rather than look complete
        "more_may_follow": any(p["key"] in blocking for p in ordered),
        "platform_app": app or None,
    }
