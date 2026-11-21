"""What Senti watches: services, each built from its own repository, the environments they run in, and their
instances. The names follow OpenTelemetry's resource attributes: system (service.namespace), service
(service.name), environment (deployment.environment.name) and instance (service.instance.id).

The setup is read from `senti.toml` in Senti's folder (home.py), or the file named by SENTI_SETUP. Secrets are never
in it: an instance names the prefix of its OIDC client settings in the environment (`credentials = "LOCAL_OIDC"`
reads LOCAL_OIDC_CLIENT_ID, LOCAL_OIDC_CLIENT_SECRET and LOCAL_OIDC_ISSUER). Without the file, the setup comes from
the environment as before (SENTINEL_INSTANCES and the settings prefixed with each instance's name), and
`senti config import-env` writes that setup to the file once.

A service's kind says what its code is: how its log lines read, what the analyst is told about it, and how a fix is
built. What Senti learns from the person's responses is kept per kind.
"""

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .home import in_home
from .kinds import KINDS

SETUP_FILE = "senti.toml"
NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")
ENV_PREFIX = re.compile(r"^[A-Z][A-Z0-9_]*$")

# Listed as instances: OBP-API (read through the obp source, and Prometheus or Loki if its service says so), and any
# service read only through Prometheus and Loki. The OBP apps are not listed: they are found in their OBP-API
# instance's app directory (discover = true), and use its OIDC client.
OBP_API = "obp-api"


class SetupError(Exception):
    pass


@dataclass(frozen=True)
class Service:
    name: str  # service.name
    kind: str
    repository: str = ""  # vcs.repository.url.full, e.g. https://github.com/OpenBankProject/OBP-API
    source: str = ""  # the local checkout Senti reads (and stages fixes in)
    build: str = ""  # its build on GitHub Actions, e.g. https://github.com/OpenBankProject/OBP-API/actions
    sonarcloud: str = ""  # its SonarCloud project key
    notes: str = ""  # added to what the analyst is told about this service
    sources: tuple[str, ...] = ()  # where its data is read from (sources/), first first; () = its kind's usual one

    @property
    def github_repo(self) -> str:
        """owner/name, if the repository is on GitHub; "" otherwise."""
        match = re.match(r"^https://github\.com/([^/]+/[^/]+?)(?:\.git)?/?$", self.repository)
        return match.group(1) if match else ""


@dataclass(frozen=True)
class Environment:
    name: str  # deployment.environment.name: local, test, staging, production, or the team's own words


@dataclass(frozen=True)
class Instance:
    id: str  # service.instance.id; also names its database (sentinel-<id>.db) and digests (digests/<id>)
    service: str
    environment: str
    url: str
    credentials: str = ""  # the prefix of its OIDC client settings in the environment
    discover: bool = True  # also watch the apps listed in its app directory, in the same environment
    prometheus: str = ""  # its series in Prometheus, as label matchers: job="obp-api",instance="localhost:9464"
    loki: str = ""  # its log streams in Loki, as a stream selector: {app="obp-api",env="local"}


@dataclass(frozen=True)
class Setup:
    system: str = ""  # service.namespace
    services: tuple[Service, ...] = ()
    environments: tuple[Environment, ...] = ()  # in order, from a developer's machine to production
    instances: tuple[Instance, ...] = ()
    path: str = ""  # the file it was read from; "" when it came from the environment
    prometheus_url: str = ""  # [prometheus] url: the Prometheus server Senti queries
    loki_url: str = ""  # [loki] url: the Loki server Senti queries
    by_service: dict = field(default_factory=dict, compare=False, repr=False)

    def __post_init__(self):
        self.by_service.update({s.name: s for s in self.services})

    def service(self, name: str) -> Service | None:
        return self.by_service.get(name)

    def instance(self, instance_id: str) -> Instance | None:
        return next((i for i in self.instances if i.id == instance_id), None)


def setup_path() -> Path:
    return Path(in_home(os.environ.get("SENTI_SETUP") or SETUP_FILE))


def _text(table: dict, key: str, where: str, required: bool = False) -> str:
    value = table.get(key, "")
    if not isinstance(value, str):
        raise SetupError(f"{where}: {key} must be text")
    if required and not value.strip():
        raise SetupError(f"{where}: {key} is missing")
    return value.strip()


def _name(table: dict, key: str, where: str) -> str:
    value = _text(table, key, where, required=True)
    if not NAME.match(value):
        raise SetupError(f"{where}: {key} {value!r} must be lower case letters, digits and -")
    return value


def _tables(data: dict, key: str) -> list[dict]:
    tables = data.get(key, [])
    if not isinstance(tables, list) or not all(isinstance(t, dict) for t in tables):
        raise SetupError(f"[[{key}]] must be a list of tables")
    return tables


def parse(data: dict, path: str = "") -> Setup:
    """A setup from the parsed TOML, checked: every name unique, every reference to something defined."""
    known = {"system", "service", "environment", "instance", "prometheus", "loki"}
    if unknown := sorted(set(data) - known):
        raise SetupError(f"Unknown section {', '.join(unknown)}: use {', '.join(sorted(known))}")
    system = data.get("system", {})
    if not isinstance(system, dict):
        raise SetupError("[system] must be a table")

    services = []
    for n, t in enumerate(_tables(data, "service"), 1):
        where = f"service {n}"
        name = _name(t, "name", where)
        where = f"service {name}"
        kind = _text(t, "kind", where, required=True)
        if kind not in KINDS:
            raise SetupError(f"{where}: kind {kind!r} is not one Senti knows ({', '.join(KINDS)})")
        sources = t.get("sources", [])
        if not isinstance(sources, list) or not all(isinstance(x, str) and NAME.match(x) for x in sources):
            raise SetupError(f'{where}: sources must be a list of source names, e.g. ["obp", "prometheus"]')
        services.append(Service(name, kind, *(_text(t, k, where) for k in
                                              ("repository", "source", "build", "sonarcloud", "notes")),
                                tuple(sources)))

    environments = [Environment(_name(t, "name", f"environment {n}"))
                    for n, t in enumerate(_tables(data, "environment"), 1)]

    instances = []
    for n, t in enumerate(_tables(data, "instance"), 1):
        where = f"instance {n}"
        service, environment = _name(t, "service", where), _name(t, "environment", where)
        instance_id = _name(t, "id", where) if "id" in t else f"{service}-{environment}"
        where = f"instance {instance_id}"
        credentials = _text(t, "credentials", where)
        if credentials and not ENV_PREFIX.match(credentials):
            raise SetupError(f"{where}: credentials {credentials!r} must be the prefix of environment variables, "
                             "in capitals, e.g. PRODUCTION_OIDC")
        discover = t.get("discover", True)
        if not isinstance(discover, bool):
            raise SetupError(f"{where}: discover must be true or false")
        instances.append(Instance(instance_id, service, environment, _text(t, "url", where, required=True).rstrip("/"),
                                  credentials, discover, _text(t, "prometheus", where), _text(t, "loki", where)))

    for label, names in (("service", [s.name for s in services]), ("environment", [e.name for e in environments]),
                         ("instance", [i.id for i in instances])):
        if len(set(names)) != len(names):
            raise SetupError(f"Two {label}s have the same name: {', '.join(sorted({x for x in names if names.count(x) > 1}))}")
    servers = {}
    for section in ("prometheus", "loki"):
        table = data.get(section, {})
        if not isinstance(table, dict):
            raise SetupError(f"[{section}] must be a table")
        servers[section] = _text(table, "url", f"[{section}]").rstrip("/")

    by_name = {s.name: s for s in services}
    for i in instances:
        if i.service not in by_name:
            raise SetupError(f"instance {i.id}: there is no service {i.service!r}")
        if i.environment not in {e.name for e in environments}:
            raise SetupError(f"instance {i.id}: there is no environment {i.environment!r}")
        service = by_name[i.service]
        if "obp-app" in service.sources:
            raise SetupError(f"instance {i.id}: OBP apps are found in their OBP-API instance's app directory "
                             "(discover = true), not listed")
        if service.kind != OBP_API and (not service.sources or "obp" in service.sources):
            raise SetupError(f"instance {i.id}: a {service.kind} service is read from Prometheus or Loki: give "
                             f"service {service.name} sources, e.g. [\"prometheus\", \"loki\"]")
        for section in ("prometheus", "loki"):
            if section in service.sources and not getattr(i, section):
                raise SetupError(f"instance {i.id}: its service reads {section}: say which of its data is this "
                                 f"instance's ({section} = ...)")
            if section in service.sources and not servers[section]:
                raise SetupError(f"[{section}] url is missing: service {service.name} reads {section}")
    return Setup(_text(system, "name", "[system]"), tuple(services), tuple(environments), tuple(instances), path,
                 servers["prometheus"], servers["loki"])


def load(path: Path | None = None) -> Setup | None:
    """The setup in senti.toml (or SENTI_SETUP), or None if there is no such file."""
    path = path or setup_path()
    if not path.is_file():
        return None
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise SetupError(f"{path} is not valid TOML: {e}") from e
    return parse(data, str(path))


def _quoted(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def to_toml(setup: Setup) -> str:
    """The setup as senti.toml. Only settings that are set are written."""
    out = ["# What Senti watches. Secrets stay in .env: each instance names the prefix of its OIDC client settings.",
           ""]
    if setup.system:
        out += ["[system]", f"name = {_quoted(setup.system)}", ""]
    for s in setup.services:
        out += ["[[service]]", f"name = {_quoted(s.name)}", f"kind = {_quoted(s.kind)}"]
        out += [f"{k} = {_quoted(getattr(s, k))}" for k in ("repository", "source", "build", "sonarcloud", "notes")
                if getattr(s, k)]
        if s.sources:
            out.append(f"sources = [{', '.join(_quoted(x) for x in s.sources)}]")
        out.append("")
    for section in ("prometheus", "loki"):
        if url := getattr(setup, f"{section}_url"):
            out += [f"[{section}]", f"url = {_quoted(url)}", ""]
    for e in setup.environments:
        out += ["[[environment]]", f"name = {_quoted(e.name)}", ""]
    for i in setup.instances:
        out += ["[[instance]]", f"id = {_quoted(i.id)}", f"service = {_quoted(i.service)}",
                f"environment = {_quoted(i.environment)}", f"url = {_quoted(i.url)}"]
        if i.credentials:
            out.append(f"credentials = {_quoted(i.credentials)}")
        out += [f"{k} = {_quoted(getattr(i, k))}" for k in ("prometheus", "loki") if getattr(i, k)]
        out += [f"discover = {'true' if i.discover else 'false'}", ""]
    return "\n".join(out)
