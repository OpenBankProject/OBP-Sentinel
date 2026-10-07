"""The `sentinel` command."""

import argparse
import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone

from .analyst import Analyst, decide, start_scheduler, unavailable_reason
from .collector import Collector
from .config import Config, ConfigError
from .digest import NotReady, write_digest
from .logs import for_instance, setup as setup_logging
from .obp_client import OBPClient
from .source import TIERS, Reviews, mark_of, next_endpoints
from .store import VERDICTS, Store, now
from .summary import build_summary, coverage, to_markdown
from .web import serve, serve_in_background

logger = logging.getLogger(__name__)


def scheduled_analyst(args, config: Config) -> Analyst | None:
    return None if args.no_analyst else start_scheduler(config)


def analysts_for(args, configs: list[Config]) -> dict[str, Analyst]:
    """One analyst per instance: on its schedule (unless --no-analyst), and for analyses asked for on the page."""
    return {c.name: scheduled_analyst(args, c) or Analyst(c) for c in configs}


def stop(analysts: dict[str, Analyst]) -> None:
    for analyst in analysts.values():
        analyst.stop()


def collect(config: Config) -> None:
    """Poll one instance forever. Runs in its own thread, with its own database connection."""
    store = Store(config.db_path)
    try:
        while True:
            try:
                Collector(config, store, OBPClient(config)).run_forever()
            except Exception:
                logger.exception("Collector failed; starting again in %ss", config.poll_seconds)
                time.sleep(config.poll_seconds)
    finally:
        store.close()


def collect_forever(configs: list[Config]) -> None:
    threads = [for_instance(threading.Thread(target=collect, args=(c,), name=f"collector-{c.name}", daemon=True),
                            c.obp_base_url) for c in configs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def cmd_collect(args, configs: list[Config]) -> None:
    if args.once:
        for config in configs:
            for_instance(threading.current_thread(), config.obp_base_url)
            store = Store(config.db_path)
            try:
                Collector(config, store, OBPClient(config)).poll_once()
            finally:
                store.close()
        return
    analysts = {c.name: a for c in configs if (a := scheduled_analyst(args, c))}
    try:
        collect_forever(configs)
    finally:
        stop(analysts)


def cmd_analyse(args, config: Config, store: Store) -> None:
    reason = unavailable_reason(config, scheduled=False)
    if reason:
        sys.exit(reason)
    go, why = decide(store, config)
    if not go and not args.force:
        sys.exit(f"{why} (use --force to run anyway)")
    run_id = Analyst(config).run("By hand: " + why)
    if run_id is None:
        sys.exit("An analysis is already running")
    run = store.db.execute("SELECT * FROM analysis_runs WHERE id = ?", (run_id,)).fetchone()
    print(f"Analysis #{run_id} {run['status']}" + (f": {run['error']}" if run["error"] else ""))
    if run["result"]:
        print(run["result"])


def cmd_status(args, config: Config, store: Store) -> None:
    c = coverage(store, config, now() - 86400, now())
    counts = store.db.execute(
        "SELECT (SELECT COUNT(*) FROM signatures) AS signatures, "
        "(SELECT COUNT(*) FROM signatures WHERE status = 'dismissed') AS dismissed, "
        "(SELECT COUNT(*) FROM findings) AS findings, "
        "(SELECT MAX(ts) FROM polls WHERE ok = 1) AS last_poll"
    ).fetchone()
    print(f"Last 24h: watched {c['watched_hours']}h, {c['polls']} polls, {c['failed_polls']} failed, "
          f"{c['polls_with_missed_entries']} with possibly missed entries")
    print(f"Signatures: {counts['signatures']} ({counts['dismissed']} dismissed), findings: {counts['findings']}")
    print(f"Last successful poll: {counts['last_poll']} (now {now()})")


def cmd_summary(args, config: Config, store: Store) -> None:
    summary = build_summary(store, config, args.hours, args.limit)
    print(json.dumps(summary, indent=2) if args.json else to_markdown(summary))


def cmd_show(args, config: Config, store: Store) -> None:
    sig = store.db.execute("SELECT * FROM signatures WHERE id = ?", (args.signature_id,)).fetchone()
    if not sig:
        sys.exit(f"No signature {args.signature_id}")
    print(json.dumps(dict(sig), indent=2))
    for s in store.db.execute("SELECT ts, message FROM samples WHERE signature_id = ? ORDER BY ts", (sig["id"],)):
        print(f"\n--- sample at ts {s['ts']} ---\n{s['message']}")


def cmd_findings(args, config: Config, store: Store) -> None:
    if args.action == "import":
        with open(args.file) as fh:
            items = json.load(fh)
        for item in items if isinstance(items, list) else [items]:
            finding_id = store.upsert_finding(item)
            print(f"#{finding_id} {item['key']}")
        store.commit()
    else:
        rows = store.findings() if args.all else store.findings(("open", "suggested", "later"))
        for f in rows:
            print(f"#{f['id']} [{f['status']}] {f['priority']:>6} {f['category']:<12} {f['key']}: {f['title']}")
            fb = store.db.execute(
                "SELECT ts, verdict, comment FROM feedback WHERE finding_id = ? ORDER BY id DESC LIMIT 1", (f["id"],)
            ).fetchone()
            if fb:
                when = datetime.fromtimestamp(fb["ts"], timezone.utc).strftime("%Y-%m-%d %H:%MZ")
                print(f"    feedback {fb['verdict']} at {when}" + (f": {fb['comment']}" if fb["comment"] else ""))


def cmd_digest(args, config: Config, store: Store) -> None:
    try:
        print(write_digest(store, config, args.out or config.digest_dir, args.force))
    except NotReady as e:
        sys.exit(str(e))


def cmd_feedback(args, config: Config, store: Store) -> None:
    store.add_feedback(args.finding_id, args.verdict, args.comment, config.snooze_days)
    store.commit()
    print(f"Finding #{args.finding_id} marked {args.verdict}")


def cmd_ignore(args, config: Config, store: Store) -> None:
    if not store.set_signature_status(args.signature_id, "dismissed"):
        sys.exit(f"No signature {args.signature_id}")
    store.commit()
    print(f"Signature {args.signature_id} will no longer appear in summaries")


def cmd_ui(args, configs: list[Config]) -> None:
    analysts = analysts_for(args, configs)
    first = configs[0]  # the page's settings are not per instance
    try:
        serve(configs, args.host or first.ui_host, args.port or first.ui_port, tuple(args.allow_host), analysts)
    finally:
        stop(analysts)


def cmd_run(args, configs: list[Config]) -> None:
    """All in one process: the web page and each instance's analyst schedule and collector, in threads."""
    analysts = analysts_for(args, configs)
    first = configs[0]
    server = serve_in_background(configs, args.host or first.ui_host, args.port or first.ui_port,
                                 tuple(args.allow_host), analysts)
    try:
        collect_forever(configs)
    finally:
        stop(analysts)
        server.shutdown()
        server.server_close()


def cmd_source(args, configs: list[Config]) -> None:
    reviews = Reviews(configs[0].source_db_path)
    try:
        if args.action == "next":
            picked, total, reviewed = next_endpoints(configs, reviews, args.n)
            if not total:
                print("No instance has listed its endpoints yet: the collector does that once a day.")
            print(f"Reviewed {reviewed} of {total} endpoints.")
            for e in picked:
                print(f"\n{e['operation_id']}: {TIERS[e['tier']]}, on {', '.join(e['instances'])}")
                print(f"  read at commit {e['read_at']}")
                print(f"  defined in {', '.join(e['handlers']) or '(not found: search for it)'}")
                if e["changed"]:
                    print(f"  reviewed before; changed since: {', '.join(e['changed'])}")
        else:
            source = configs[0].obp_api_source
            if args.action == "done":
                marks = {unit: mark_of(source, args.commit, unit, args.operation_id) for unit in args.paths}
                if problems := [problem for _, problem in marks.values() if problem]:
                    sys.exit("Nothing recorded:\n" + "\n".join(problems))
                reviews.record(args.operation_id, args.commit, {unit: mark for unit, (mark, _) in marks.items()})
                print(f"Recorded {args.operation_id}: {len(marks)} files and functions")
            else:
                for unit in args.paths:
                    print(seen_line(source, args.commit, reviews, unit))
    finally:
        reviews.close()


def seen_line(source: str, commit: str, reviews: Reviews, unit: str) -> str:
    """Whether a file, or a function in a big file, was already reviewed and is unchanged."""
    mark, problem = mark_of(source, commit, unit)
    if problem:
        if "#" in unit or not (functions := reviews.functions_reviewed_in(unit)):
            return f"{unit}: {problem}"
        states = [f"{f.partition('#')[2]} ({'unchanged' if reviews.file(mark_of(source, commit, f)[0] or '') else 'changed'})"
                  for f in functions]
        return f"{unit}: {problem}. Reviewed in it so far: {', '.join(states)}"
    row, last = reviews.file(mark), reviews.last_review_of(unit)
    return f"{unit}: " + (f"reviewed, unchanged (for {row['operation_id']})" if row
                          else f"changed since its review for {last['operation_id']}" if last else "not reviewed")


def pick_instance(configs: list[Config], name: str | None) -> Config:
    """The instance a one-instance command works on: --instance, else SENTINEL_INSTANCE, else the only one."""
    name = name or os.environ.get("SENTINEL_INSTANCE")
    names = ", ".join(c.name for c in configs)
    if name:
        for c in configs:
            if c.name == name:
                return c
        sys.exit(f"No instance {name!r}; SENTINEL_INSTANCES has: {names}")
    if len(configs) > 1:
        sys.exit(f"Several instances ({names}): choose one with --instance")
    return configs[0]


def add_ui_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--host", help="address the page listens on (default SENTINEL_UI_HOST, 127.0.0.1)")
    p.add_argument("--port", type=int, help="port of the page (default SENTINEL_UI_PORT, 8765)")
    p.add_argument("--allow-host", action="append", default=[], metavar="NAME",
                   help="another host name the page is reached by, e.g. behind a proxy (repeatable)")
    add_analyst_argument(p)


def add_analyst_argument(p: argparse.ArgumentParser) -> None:
    p.add_argument("--no-analyst", action="store_true",
                   help="do not run the analyst at startup and every SENTINEL_ANALYSE_MINUTES")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="sentinel", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--instance", help="the OBP-API instance to work on, from SENTINEL_INSTANCES "
                                           "(default SENTINEL_INSTANCE; run, collect and ui default to all)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("run", help="start Sentinel: the collector, the web page and the analyst's schedule, in one process")
    add_ui_arguments(p)
    p.set_defaults(func=cmd_run, all_instances=True)

    p = sub.add_parser("collect", help="only the collector: poll OBP-API and record what it sees")
    p.add_argument("--once", action="store_true", help="poll once and exit")
    add_analyst_argument(p)
    p.set_defaults(func=cmd_collect, all_instances=True)

    p = sub.add_parser("analyse", help="run the analyst now (headless Claude Code), if there is something new")
    p.add_argument("--force", action="store_true", help="run even if nothing is new")
    p.set_defaults(func=cmd_analyse)

    sub.add_parser("status", help="how well Sentinel has been watching").set_defaults(func=cmd_status)

    p = sub.add_parser("summary", help="aggregates for the analyst")
    p.add_argument("--hours", type=float, default=6)
    p.add_argument("--limit", type=int, default=30, help="most signatures to include")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_summary)

    p = sub.add_parser("show", help="a signature and its raw samples")
    p.add_argument("signature_id")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("findings", help="list findings, or import the analyst's findings")
    p.add_argument("action", choices=("list", "import"))
    p.add_argument("file", nargs="?", help="JSON file (for import)")
    p.add_argument("--all", action="store_true", help="include accepted, acted on, dismissed and fixed")
    p.set_defaults(func=cmd_findings)

    p = sub.add_parser("digest", help="write the next digest of top suggestions")
    p.add_argument("--out", help="directory (default SENTINEL_DIGEST_DIR: digests, or digests/<instance>)")
    p.add_argument("--force", action="store_true", help="skip the minimum watch time")
    p.set_defaults(func=cmd_digest)

    p = sub.add_parser("feedback", help="respond to a suggestion")
    p.add_argument("finding_id", type=int)
    p.add_argument("verdict", choices=VERDICTS)
    p.add_argument("--comment")
    p.set_defaults(func=cmd_feedback)

    p = sub.add_parser("ignore", help="never show a signature again")
    p.add_argument("signature_id")
    p.set_defaults(func=cmd_ignore)

    p = sub.add_parser("source", help="the source review: what to review next, and what has been reviewed")
    p.add_argument("action", choices=("next", "done", "seen"))
    p.add_argument("operation_id", nargs="?", help="the endpoint reviewed (for done)")
    p.add_argument("paths", nargs="*", help="files read, relative to the OBP-API checkout; for a file of more than "
                                            "500 lines, each function read in it as path#name (for done and seen)")
    p.add_argument("--commit", help="the commit the files were read at (for done and seen)")
    p.add_argument("--n", type=int, default=3, help="how many endpoints to list (for next)")
    p.set_defaults(func=cmd_source, all_instances=True)

    p = sub.add_parser("ui", help="only the web page to read findings and respond to them")
    add_ui_arguments(p)
    p.set_defaults(func=cmd_ui, all_instances=True)

    args = parser.parse_args(argv)
    if args.command == "findings" and args.action == "import" and not args.file:
        parser.error("findings import needs a file")
    if args.command == "source" and args.action != "next":
        if not args.commit:
            parser.error(f"source {args.action} needs --commit")
        if args.action == "seen" and args.operation_id:  # seen takes paths only
            args.paths.insert(0, args.operation_id)
        if not args.paths:
            parser.error(f"source {args.action} needs the files' paths")
    setup_logging(args.verbose)
    try:
        configs = Config.all_from_env()
    except ConfigError as e:
        sys.exit(str(e))
    if getattr(args, "all_instances", False):
        try:
            args.func(args, [pick_instance(configs, args.instance)] if args.instance else configs)
        except KeyboardInterrupt:
            pass
        return
    config = pick_instance(configs, args.instance)
    for_instance(threading.current_thread(), config.obp_base_url)
    store = Store(config.db_path)
    try:
        args.func(args, config, store)
    except KeyboardInterrupt:
        pass
    finally:
        store.close()


if __name__ == "__main__":
    main()
