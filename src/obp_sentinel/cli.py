"""The `sentinel` command."""

import argparse
import json
import logging
import sys

from .collector import Collector
from .config import Config
from .digest import NotReady, write_digest
from .obp_client import OBPClient, OBPError
from .store import Store, now
from .summary import build_summary, coverage, to_markdown

logger = logging.getLogger(__name__)

VERDICTS = ("accepted", "dismissed", "later", "fixed")


def declare_platform_app(client: OBPClient) -> None:
    """Tell OBP-API which Scopes Sentinel needs, and say which are missing. Polling goes ahead either way."""
    try:
        app = client.declare_platform_app()
    except OBPError as e:
        logger.warning("Could not declare Sentinel's Scopes (is its Consumer marked as a Platform App?): %s", e)
        return
    missing = [s["role_name"] for s in app.get("required_scopes", []) if not s.get("held") and not s.get("optional")]
    if missing:
        logger.warning("Consumer %s lacks the Scopes %s; ask an administrator to grant them",
                       app.get("consumer_id"), ", ".join(missing))
    else:
        logger.info("Declared as Platform App %r; all Scopes held", app.get("label"))


def cmd_collect(args, config: Config, store: Store) -> None:
    client = OBPClient(config)
    declare_platform_app(client)
    collector = Collector(config, store, client)
    if args.once:
        collector.poll_once()
    else:
        collector.run_forever()


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


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="sentinel", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("collect", help="poll OBP-API and record what it sees")
    p.add_argument("--once", action="store_true", help="poll once and exit")
    p.set_defaults(func=cmd_collect)

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
    p.add_argument("--all", action="store_true", help="include accepted, dismissed and fixed")
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
