# OBP-Sentinel

A long-running companion to OBP-API. It watches a running instance for hours, works out what is
actually going wrong or getting slower, and recommends **the few improvements that matter most**,
without overwhelming developers with suggestions.

## How it works

```
┌──────────────┐   every 2 min      ┌──────────────┐   every few hours  ┌──────────────┐
│  Collector   │ ─────────────────▶ │   SQLite     │ ─────────────────▶ │   Analyst    │
│  (plain code,│  log cache,        │  (memory)    │  aggregates,       │  (Claude)    │
│   no LLM)    │  Telemetry         │              │  trends, history   │              │
└──────────────┘                    └──────────────┘                    └──────┬───────┘
                                          ▲                                    │
                                          │  feedback (accepted / dismissed)   ▼
                                          └──────────────────────────── digest: top 1–3
```

- **Collector** (`sentinel collect`): polls `GET /obp/v5.1.0/system/log-cache/{level}`,
  `GET /obp/v7.0.0/management/telemetry` and `GET /obp/v6.0.0/management/aggregate-metrics`. Log lines are normalised into **signatures**: ids, numbers,
  quoted values, UUIDs, emails and IPs are stripped, so one problem is one signature. Each signature is
  counted per 15-minute bucket. Telemetry counters are stored as snapshots so rates and latencies can be
  compared between windows. Aggregate metrics (call count, response times, distinct users, consumers
  and consents) are fetched once per bucket after it ends.
- **Analyst** (the Claude Code subagent in `.claude/agents/obp-sentinel-analyst.md`): reads
  `sentinel summary` (aggregates over the last hours compared with the hours before), investigates the
  OBP-API source, and records findings scored by impact, confidence, effort and trend.
- **Digest** (`sentinel digest`): the only thing people see. It is written only after enough hours of
  watching, holds at most 3 suggestions (and 10 a week), skips anything below the priority threshold or
  seen in only one bucket, and never repeats what was suggested, dismissed or fixed. When nothing
  qualifies it says so.
- **Feedback** (`sentinel feedback`): accepted / dismissed / later / fixed. Dismissing a finding also
  hides its signatures from future summaries.

At this stage Sentinel is read-only towards OBP-API: it does not change log levels, edit source or open
pull requests.

## Setup

1. In OBP-API's props, enable the log cache (it needs Redis):

   ```properties
   redis_logging_enabled = true
   redis_logging_min_level = INFO
   ```

2. Sentinel calls OBP-API as a **Platform App**, with its own application token (OAuth2 client
   credentials from OBP-OIDC), so it needs no OBP user or password.
   - OBP-OIDC creates the client `obp-sentinel` at startup. Copy its client id and secret into `.env`.
   - The first call with that token creates its Consumer in OBP-API. An administrator marks that
     Consumer as a Platform App (`POST /obp/v7.0.0/management/platform-apps`).
   - While `sentinel collect` runs, Sentinel declares the Scopes it needs
     (`PUT /obp/v7.0.0/consumers/current/platform-app`), the way the Portal and API Manager do from
     their `/status` check. OBP refuses this until the Consumer is marked, so Sentinel retries on every
     poll until accepted (no restart needed), then re-declares hourly. It declares
     `CanGetSystemLogCache<Level>` for each level in `SENTINEL_LOG_LEVELS`, `CanGetTelemetry` and `CanReadAggregateMetrics`. It logs any that are missing. The administrator
     sees them in `GET /obp/v7.0.0/management/platform-apps` and grants them
     (`POST /obp/v7.0.0/consumers/CONSUMER_ID/scopes`).

3. Configure and install:

   ```bash
   cp .env.example .env    # then edit
   uv sync
   uv run sentinel collect --once   # check that it can reach OBP-API
   uv run sentinel status
   ```

## Running

```bash
uv run sentinel collect                 # leave running; polls every SENTINEL_POLL_SECONDS
```

After a few hours, open Claude Code in this directory and ask:

> Use the obp-sentinel-analyst agent to produce a digest.

Then read `digests/<timestamp>.md` and respond to each suggestion:

```bash
uv run sentinel feedback 3 accepted --comment "Will fix in the consent refactor"
uv run sentinel feedback 4 dismissed --comment "Expected when clients send bad IBANs"
```

A quiet localhost gives Sentinel nothing to find: run OBP-load-tester, OBP-End-To-End-Testing or
OBP-Sandbox-Populator against it.

## Commands

| Command | What it does |
|---|---|
| `sentinel collect [--once]` | Poll the log cache, Telemetry and aggregate metrics |
| `sentinel status` | How well Sentinel has watched over the last 24h |
| `sentinel summary [--hours 6] [--json]` | Aggregates for the analyst |
| `sentinel show <signature>` | A signature and its raw samples |
| `sentinel findings list [--all]` / `import <file>` | The analyst's findings |
| `sentinel digest [--force]` | Write the next digest |
| `sentinel feedback <id> accepted\|dismissed\|later\|fixed` | Respond to a suggestion |
| `sentinel ignore <signature>` | Never show a signature again |

## Notes

- The log cache keeps the newest 1000 entries per level and has no ids. The collector remembers the
  newest few messages it saw and counts only what is above them next time. If they were trimmed away
  before the next poll (a burst of more than 1000 entries), the poll is flagged as possibly missing
  entries; poll more often if that happens.
- Sentinel's own calls are ignored via `SENTINEL_IGNORE_REGEX`. They are still counted in the aggregate
  metrics (the v6.0.0 endpoint has no exclude filters), a handful of calls per poll.
- OBP-API masks sensitive values before writing to the log cache (`SecureLogging.maskSensitive`), and
  Telemetry tags never identify people. Still, treat `sentinel.db` as containing log data.

## Next stages

1. Run the analyst on a schedule.
2. Let the analyst raise the log level for an area it is investigating, then restore it.
3. Let it open pull requests (against a fork or branch, never `main`) for findings a person accepted.

## License

Copyright (C) 2026, TESOBE GmbH. Licensed under the GNU Affero General Public License v3.0 or later;
see [LICENSE](LICENSE) and [NOTICE](NOTICE).
