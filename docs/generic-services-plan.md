# Plan: Senti watches any service, not only OBP

Today Senti's model is "an OBP-API instance, and the apps in its app directory". This plan makes the model "services, each from its own repository, each with several instances", and moves everything OBP-specific behind an adapter. OBP Senti then becomes TESOBE Senti plus the OBP adapter, which matches the two editions on senti.tesobe.com.

The project, package and command are renamed to Senti (`senti` to `senti`, `senti` to `senti`). Apart from that, nothing here changes how it behaves for OBP. Each phase can be finished and used on its own.

## What is tied to OBP today

| Area | Where | What is OBP-specific |
|---|---|---|
| Model and settings | `config.py` | An instance is an OBP-API instance. Apps are a fixed list (`APPS`: explorer, manager, portal, cohort, oidc, populator) with their app directory key, monitoring path, source setting (`EXPLORER_SOURCE`, `FRONTEND_SOURCE`, `OIDC_SOURCE`) and GitHub repository. Field names say OBP: `obp_base_url`, `obp_api_source`, `obp_api_github_repo`, `obp_api_url`. Default telemetry prefixes and ignore regex are written for OBP-API. |
| Discovery | `config.py` `discover_apps` | Apps come from `GET /obp/v6.0.0/app-directory`, and are named `<instance>-<app>`. |
| Fetching data | `obp_client.py`, `collector.py` | OBP endpoints and versions: log cache, Telemetry, aggregate metrics, root (for the running commit), resource docs and reachable Roles. Token by OIDC client credentials. |
| Access | `obp_client.py`, `collector.py`, `health.py` | Platform App declaration, the Roles Senti needs as Scopes, `/my/roles`, and health messages written in OBP terms. |
| Log lines | `signatures.py` | Knows OBP-API's, the JavaScript apps' and OBP-OIDC's line formats, and normalises `/obp/vX.Y.Z/...` paths. |
| Summary | `summary.py` | Reads the meters `obp.api.endpoint.requests` and `obp.api.connector.calls`; the aggregate metrics section is OBP-API's. |
| Source review | `source.py`, `.claude/agents/senti-source-reviewer.md` | Scala source under `obp-api/src/main/scala`, endpoints as `OBPEndpoint`/`HttpRoutes`, reachability from resource docs and Roles, OBP error codes. |
| Fixes | `fixer.py` | Maven with `obp-commons,obp-api`, the folders a fix may change, OBP-API props, the Lift/http4s parity scripts, `ErrorMessages`. |
| History | `history.py`, `lift-reference.md` | The Lift to http4s move in May 2026. |
| Analyst and replies | `.claude/agents/senti-analyst.md`, `analyst.py`, `replies.py`, `classes.py` | The prompts talk about OBP-API and its apps. |
| Web page | `web.py`, `ui.html` | One tab per OBP-API instance with its apps after it; help text read from OBP-API's Glossary. |
| Build and SonarCloud | `build.py`, `sonar.py` | Already general, but set once for OBP-API (`OBP_API_BUILD_ACTIONS_URL`, `SONARCLOUD_PROJECT`) rather than per service. |

Already general: the store, the digest rules, preferences and learning, rechecks, the scanner's scheduling, budgets, and running Claude Code.

## The standard names

Senti uses OpenTelemetry's resource attribute names for its own model, so its words match what operations teams already use, and data from Prometheus, Loki or an OpenTelemetry Collector maps straight in.

| Senti | OpenTelemetry | OBP example |
|---|---|---|
| System (a group of services) | `service.namespace` | `obp` |
| Service | `service.name` | `obp-api`, `api-explorer`, `obp-oidc` |
| Instance | `service.instance.id` | `obp-api-production` |
| Environment | `deployment.environment.name` | `local`, `test`, `staging`, `production` |
| Running version | `service.version`, `vcs.ref.head.revision` | the commit |
| Repository | `vcs.repository.url.full` | `https://github.com/OpenBankProject/OBP-API` |

Environment names are free text: teams use their own (pre-production, UAT and so on). Senti only needs to know their order, from local to production, to sort them on the page.

## The new model

### Kind and data source: two separate things

Each service has two settings that today are one:

- **Kind:** what the code is and how it fails. It decides how log lines are parsed, which notes the analyst, reviewer and fixer get, how a fix is built and tested, how endpoints and their reachability are found, and what Senti learns. Learning (ratings, dismissals, acted on) is kept per kind, so what the team thinks of SvelteKit findings does not change the ranking of Scala findings.
- **Data source:** where Senti reads the logs, metrics and running commit from. OBP's own endpoints are one source; Prometheus and Loki are another. One service can use several (logs from OBP-API's log cache, metrics from Prometheus).

Examples of kinds:

| Kind | What it is | OBP services of this kind |
|---|---|---|
| `obp-api` | OBP-API: Scala, http4s with Lift history, resource docs, Roles, connectors, Maven | OBP-API |
| `scala-http4s` | A Scala service on http4s and cats-effect, built with sbt | OBP-OIDC |
| `sveltekit` | A SvelteKit app on Node: server routes, sessions, calls to an API | API Manager, Portal, Cohort, Sandbox Populator |
| `vue-express` | A Vue single-page app with an Express server | API Explorer II |
| `spring-boot` | Java or Kotlin on Spring Boot, with Actuator and Micrometer | none; common in banks |
| `node-api` | An Express or NestJS API | none |
| `python-web` | Django or FastAPI | none |
| `go-service` | A Go HTTP service | none |
| `worker` | A queue consumer or scheduled job, with no endpoints | none (the Rabbit adapter would fit) |
| `website` | A static or server-rendered site | senti.tesobe.com |

A kind can be general (`sveltekit`) while a service adds its own notes in `senti.toml` ("calls OBP-API; an OBP error it passes on belongs to OBP-API"). So OBP Senti is TESOBE Senti with the `obp-api` kind, the OBP data source and notes for each OBP app.

Examples of data sources:

| Data source | Logs | Metrics | Running commit | Finds other instances |
|---|---|---|---|---|
| `obp` | OBP-API's log cache | Telemetry and aggregate metrics | root endpoint | the app directory |
| `obp-app` | the app's `/monitoring/log-cache` | the app's `/monitoring/telemetry` | Telemetry's `git_commit` | none |
| `prometheus` | none | PromQL over the HTTP query API | the `service.version` or `git_commit` label | targets with the same `job` |
| `loki` | LogQL over the HTTP query API | none | none | none |

OBP-API already records its Telemetry with Micrometer, served for Prometheus (`docs/telemetry_conventions.md` in OBP-API), and keeps standard names such as `http_server_requests_seconds`. So the Prometheus source reads OBP-API and any Spring Boot service with the same queries.

### Settings file

The settings move from prefixed environment variables (`STAGING_OBP_BASE_URL`) to one file, `senti.toml`. Python 3.11 reads TOML without a new dependency. Secrets stay in the environment or `.env`, and the file names them.

```toml
[system]
name = "obp"

[[service]]
name = "obp-api"
kind = "obp-api"
sources = ["obp"]
repository = "https://github.com/OpenBankProject/OBP-API"
source = "/srv/work/OBP-API"
build = "https://github.com/OpenBankProject/OBP-API/actions"
sonarcloud = "OpenBankProject_OBP-API"

[[service]]
name = "api-manager"
kind = "sveltekit"
sources = ["obp-app"]
repository = "https://github.com/OpenBankProject/OBP-Frontend"
source = "/srv/work/OBP-Frontend"
path = "apps/api-manager"
notes = "Calls OBP-API. An OBP error it only passes on belongs to OBP-API."

[[environment]]
name = "local"

[[environment]]
name = "production"

[[instance]]
service = "obp-api"
environment = "production"
url = "https://api.example.com"
credentials = "PRODUCTION_OIDC"   # reads PRODUCTION_OIDC_CLIENT_ID and PRODUCTION_OIDC_CLIENT_SECRET
discover = true                   # also add the apps in its app directory, in the same environment

[prometheus]
url = "https://prometheus.example.com"

[backstage]
catalog = "https://backstage.example.com"   # optional: services, repositories and owners from the catalogue
```

An instance's id is `<service>-<environment>` unless it is given one, so the database and digest names stay readable: `senti-obp-api-production.db`.

### The interfaces

A data source offers any of: `fetch_logs(level)`, `fetch_telemetry()`, `fetch_metrics(window)`, `running_commit()`, `discover(instance)`, and `access` (declaring and checking what Senti may read, with health messages). OBP's Platform App and Scopes live in the `obp` source.

A kind offers any of: `parse_line(raw)`, `reach()` (the endpoints served and how reachable each is), `summary_sections(...)`, `toolchain` (build and test commands, folders a fix may change, extra fix steps) and `notes` (Markdown added to the prompts). A service can override the toolchain in `senti.toml`.

Senti uses what is offered. A `worker` has no `reach()`, so it gets no source review by endpoint; a service with only Loki gets no metrics comparison.

The generic agents (`senti-analyst`, `senti-source-reviewer`) know nothing about OBP. They are told each service's kind, repository and environment, and given the kind's and the service's notes.

## Phases

### 1. The rename and the model, with no change in behaviour

- Rename the repository's package to `senti` and the command to `senti`; rename the agent files to `senti-analyst` and `senti-source-reviewer`.
- Add `Service`, `Environment` and `Instance` to `config.py`, read from `senti.toml`.
- Rename the OBP-named fields: `obp_base_url` to `url`, `obp_api_source` to `source`, `obp_api_github_repo` to `repository`, and `is_app` to the service's kind and data source.
- Move the build page and SonarCloud settings onto the service.
- Add `config import-env`, which writes `senti.toml` from today's `.env` once. Each instance keeps its name as its id, so its database and digests stay where they are; its environment can be renamed in the file. With `senti.toml` present, the instances, URLs, OIDC clients and checkouts are read from it; the tuning settings (`SENTINEL_*`) still come from `.env`. Done, except the rename.
- Keep learning per kind: each instance's kind comes from its service, and learning pools only the instances of one kind, so existing ratings count for the kind they were made on. Done.

### 2. The OBP pack, sources and kinds

Done:

- `sources/` holds the general data sources, and `kinds/` the general kinds (`sveltekit`, `vue-express`, `scala-http4s`). Each source offers what it can (`logs`, `telemetry`, `metrics`, `commit`, `reach`), and the collector polls whatever the instance's source offers and records what comes back; it no longer knows OBP endpoints.
- `packs/obp/` holds everything OBP, and registers its sources and kind: `api_source.py` (the `obp` source: OBP-API, formerly `obp_client.py`, with the Platform App declaration and the endpoint list for the source review), `app_source.py` (the `obp-app` source: an app's `/monitoring` endpoints, with the check of Roles an app refuses), `app_directory.py` (the OBP apps), `kind.py` (the `obp-api` kind and the OBP log format) and `lift-reference.md`. TESOBE Senti is everything outside `packs/`; OBP Senti is TESOBE Senti with `packs/obp`.
- Each kind has its log format: how a line is laid out, how an exception is named and what a request path looks like. The general kinds use the OBP log format for now, so the OBP apps' signature ids are unchanged (checked against every stored sample).

Moved to phase 3, because they belong with the prompts and the fix toolchain: the OBP parts of `summary.py` (the OBP meters and aggregate metrics), `source.py` (Scala endpoints, Roles, OBP error codes), `fixer.py` (Maven, parity scripts) and `history.py` (Lift).

### 3. Generic prompts and fixes

Done:

- Every AI run is told which service it works on (`context.py`): its name, kind, instance, environment and source, then the kind's notes and the service's own notes. The kinds' notes are in `kinds/notes/` and `packs/obp/notes/obp-api.md`; every OBP app gets `packs/obp/notes/obp-app.md` as its service notes, with its folder in OBP-Frontend. A service's `notes` in `senti.toml` replace them.
- The analyst agent is generic: where the code is, Lift, Roles and OBP error codes moved into the `obp-api` notes. Replies, bug classes and Trace history say "the service"; Trace history takes the kind's history notes (OBP-API's: Lift).
- Fix this takes its build, compile and test commands, the folders a fix may change, the scripts it may run, the ignored files the build needs and its rules from the kind's toolchain (`packs/obp/toolchain.py` for OBP-API). A kind without a toolchain says Fix this is not set up for it yet.
- The analyst's summary names the service, kind and environment, and leaves out the calls section when the source offers no metrics.

Still OBP-only, until a second kind needs them: the source review (`source.py` and the `senti-source-reviewer` agent: Scala endpoints, Roles), which only the `obp` source offers; the bug classes Senti starts from (`classes.SEEDS`); the Telemetry sections of the summary (phase 5); and the health page's OBP wording.

### 4. The web page

- Show services against environments, as on senti.tesobe.com: one row per service, one column per environment, each cell an instance with its commit and how many findings it has. A cell opens the instance's tab as today.
- Help text comes with Senti rather than from OBP-API's Glossary; the OBP source can add a link to the Glossary item.

### 5. Prometheus and Loki

- A `prometheus` source: request rates, error rates and latency percentiles per route from the standard HTTP server metrics, plus memory, CPU and connection pools, compared hour with hour as today.
- A `loki` source for logs, so services without OBP's log cache can be watched. It also avoids missing lines under load, which polling the last 1,000 lines of a log cache can.
- Try both on OBP-API first, next to the `obp` source, and compare what each finds.

### 6. Backstage

- Read services, their repositories, owners, kinds (from `spec.type` and tags) and the systems they belong to from a Backstage catalogue, so a company that keeps one does not list its services twice. `senti.toml` still says which instances to watch and adds what Backstage does not know.
- Show each finding's owner from the catalogue, so the briefing can go to the team that owns the code.

## Decisions

1. Rename to Senti now: package, command and agents (phase 1).
2. Prometheus is the first non-OBP data source, with Loki for logs (phase 5). OBP-API already serves its Telemetry for Prometheus.
3. Read Backstage catalogues (phase 6).
4. Learning is kept separately per kind (phase 1).
