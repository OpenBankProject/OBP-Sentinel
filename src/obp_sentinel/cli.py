"""The `sentinel` command."""

import argparse
import json
import logging
import sys
from datetime import datetime, timezone

from .analyst import Analyst, decide, start_scheduler, unavailable_reason
from .collector import Collector
from .config import Config
from .digest import NotReady, write_digest
from .obp_client import OBPClient
from .store import VERDICTS, Store, now
from .summary import build_summary, coverage, to_markdown
from .web import serve, serve_in_background

logger = logging.getLogger(__name__)


def scheduled_analyst(args, config: Config) -> Analyst | None:
    return None if args.no_analyst else start_scheduler(config)


def stop(analyst: Analyst | None) -> None:
    if analyst:
        analyst.stop()


def cmd_collect(args, config: Config, store: Store) -> None:
    collector = Collector(config, store, OBPClient(config))
    if args.once:
        collector.poll_once()
        return
    analyst = scheduled_analyst(args, config)
    try:
        collector.run_forever()
    finally:
        stop(analyst)


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
        print(write_digest(store, config, args.out, args.force))
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


def cmd_ui(args, config: Config, store: Store) -> None:
    analyst = scheduled_analyst(args, config) or Analyst(config)  # also runs analyses asked for on the page
    try:
        serve(config, args.host or config.ui_host, args.port or config.ui_port, tuple(args.allow_host), analyst)
    finally:
        stop(analyst)


def cmd_run(args, config: Config, store: Store) -> None:
    """All in one process: the web page and the analyst's schedule in the background, the collector in the foreground."""
    analyst = scheduled_analyst(args, config) or Analyst(config)  # also runs analyses asked for on the page
    server = serve_in_background(config, args.host or config.ui_host, args.port or config.ui_port,
                                 tuple(args.allow_host), analyst)
    try:
        Collector(config, store, OBPClient(config)).run_forever()
    finally:
        stop(analyst)
        server.shutdown()
        server.server_close()


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
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("run", help="start Sentinel: the collector, the web page and the analyst's schedule, in one process")
    add_ui_arguments(p)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("collect", help="only the collector: poll OBP-API and record what it sees")
    p.add_argument("--once", action="store_true", help="poll once and exit")
    add_analyst_argument(p)
    p.set_defaults(func=cmd_collect)

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
    p.add_argument("--out", default="digests")
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

    p = sub.add_parser("ui", help="only the web page to read findings and respond to them")
    add_ui_arguments(p)
    p.set_defaults(func=cmd_ui)

    args = parser.parse_args(argv)
    if args.command == "findings" and args.action == "import" and not args.file:
        parser.error("findings import needs a file")
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    config = Config.from_env()
    store = Store(config.db_path)
    try:
        args.func(args, config, store)
    except KeyboardInterrupt:
        pass
    finally:
        store.close()


if __name__ == "__main__":
    main()
