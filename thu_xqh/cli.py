"""Command line entry point."""

from __future__ import annotations

import argparse
import os
import sys
from typing import List, Optional

from . import discover as discovery
from . import ids as idlib
from .client import BudgetExhausted, DEFAULT_USER_AGENT, PoliteClient, TransportError
from .download import download_all, verify_dir
from .manifest import ERROR, MISSING, OK, Manifest

DEFAULT_BASE_URL = "https://thujournal.lib.tsinghua.edu.cn"
DEFAULT_DEST = "pdfs"
DEFAULT_IDS_FILE = "issue-ids.txt"


def build_parser() -> argparse.ArgumentParser:
    # Shared options live on a parent parser so they can be written after the
    # subcommand, which is where people reach for them.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--base-url", default=DEFAULT_BASE_URL)
    common.add_argument(
        "--delay", type=float, default=2.0,
        help="seconds between requests, jittered (default: 2.0)",
    )
    common.add_argument("--timeout", type=float, default=45.0)
    common.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    common.add_argument(
        "--max-requests", type=int, default=None,
        help="stop after this many requests; rerun later to continue",
    )
    common.add_argument("--manifest", default=None, help="default: <dest>/manifest.json")
    common.add_argument("--dest", default=DEFAULT_DEST, help="download directory")
    common.add_argument(
        "--ignore-robots", action="store_true",
        help="proceed even if robots.txt disallows the path",
    )
    common.add_argument("--first", type=int, default=idlib.DEFAULT_FIRST)
    common.add_argument("--last", type=int, default=idlib.DEFAULT_LAST)
    common.add_argument("-q", "--quiet", action="store_true")

    parser = argparse.ArgumentParser(
        prog="python -m thu_xqh",
        description="Politely archive 新清华 (XQH) issue PDFs from the Tsinghua journal server.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Typical run:\n"
            "  python -m thu_xqh discover --out issue-ids.txt\n"
            "  python -m thu_xqh download --ids issue-ids.txt --dest pdfs\n"
            "  python -m thu_xqh verify --dest pdfs\n"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    find = subparsers.add_parser(
        "discover", parents=[common], help="build the list of issue ids that exist"
    )
    find.add_argument("--out", default=DEFAULT_IDS_FILE)
    find.add_argument("--no-catalog", action="store_true", help="skip the browse-index walk")
    find.add_argument(
        "--only-discovered", action="store_true",
        help="write only the ids the site confirmed, instead of the whole base "
             "range; saves requests later if the catalog walk went well",
    )
    find.add_argument(
        "--max-level", default="year-month",
        choices=["all", "month", "year", "year-month"],
        help="how far to escalate the catalog walk (default: year-month)",
    )
    find.add_argument("--first-year", type=int, default=discovery.DEFAULT_FIRST_YEAR)
    find.add_argument("--last-year", type=int, default=discovery.DEFAULT_LAST_YEAR)
    find.add_argument("--coverage-target", type=float, default=0.98)
    find.add_argument(
        "--confirm-candidates", action="store_true",
        help="existence-check id-shaped tokens that were not found in a PDF link",
    )
    find.add_argument(
        "--probe-bases", default="none",
        help="fallback guessing: 'none' (default), 'all', 'found', a comma list, or @file",
    )
    find.add_argument(
        "--probe-suffixes", default="Z{1..30}",
        help="supplement markers to try, e.g. 'Z{1..30},S{1..4}' (default: Z{1..30})",
    )
    find.add_argument(
        "--stop-after-misses", type=int, default=0,
        help="abandon a base after N consecutive misses (0 = try every suffix)",
    )
    find.add_argument(
        "--save-html", default=None, metavar="DIR",
        help="save fetched index pages here, for checking what the parser saw",
    )
    find.add_argument("--dry-run", action="store_true", help="print the request budget and exit")
    find.add_argument("-y", "--yes", action="store_true", help="do not prompt before probing")

    get = subparsers.add_parser("download", parents=[common], help="fetch the PDFs")
    get.add_argument("--ids", default=None, help="id list file (default: the full base range)")
    get.add_argument("--limit", type=int, default=None, help="download at most N new files")
    get.add_argument("--recheck-missing", action="store_true")
    get.add_argument(
        "--force", action="store_true",
        help="re-fetch even ids the manifest already records as done",
    )
    get.add_argument("--dry-run", action="store_true")

    subparsers.add_parser("verify", parents=[common], help="re-check downloaded files on disk")
    subparsers.add_parser("status", parents=[common], help="summarise manifest progress")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    log = (lambda msg: None) if args.quiet else print
    manifest_path = args.manifest or os.path.join(args.dest, "manifest.json")

    if args.command == "status":
        return cmd_status(args, manifest_path, log)
    if args.command == "verify":
        return cmd_verify(args, manifest_path, log)

    client = PoliteClient(
        args.base_url,
        delay=args.delay,
        timeout=args.timeout,
        user_agent=args.user_agent,
        max_requests=args.max_requests,
        logger=log,
    )
    try:
        with client:
            if not _robots_ok(client, args, log):
                return 2
            if args.command == "discover":
                return cmd_discover(client, args, log)
            return cmd_download(client, args, manifest_path, log)
    except KeyboardInterrupt:
        log("\nInterrupted. Progress is saved -- rerun the same command to resume.")
        return 130
    except BudgetExhausted as exc:
        log(f"\nStopped: {exc}. Progress is saved -- rerun to continue.")
        return 0
    except TransportError as exc:
        log(f"\nStopped: {exc}")
        return 1


def _robots_ok(client: PoliteClient, args, log) -> bool:
    if args.ignore_robots:
        return True
    try:
        allowed = client.robots_allows("/swfPath/xqh/0001.pdf")
    except Exception as exc:
        log(f"Could not read robots.txt ({exc}); continuing.")
        return True
    if not allowed:
        log(
            "robots.txt disallows /swfPath/xqh/ for this user agent.\n"
            "Refusing to crawl. Re-run with --ignore-robots only if you have "
            "permission from the site operator."
        )
    return allowed


def cmd_discover(client: PoliteClient, args, log) -> int:
    found: set = set()
    supplements: set = set()

    if not args.no_catalog:
        log("Walking the browse index...")
        catalog = discovery.catalog_scan(
            client,
            first=args.first,
            last=args.last,
            first_year=args.first_year,
            last_year=args.last_year,
            coverage_target=args.coverage_target,
            max_level=args.max_level,
            save_html=args.save_html,
            log=log,
        )
        found |= catalog.confirmed
        supplements |= {i for i in catalog.confirmed if idlib.is_supplement(i)}

        weak = idlib.sorted_ids(catalog.candidates)
        if weak:
            log(f"\n{len(weak)} id-shaped token(s) were not backed by a PDF link.")
            weak_supplements = [i for i in weak if idlib.is_supplement(i)]
            if weak_supplements:
                log(f"  of which {len(weak_supplements)} look like supplements: "
                    f"{', '.join(weak_supplements[:20])}"
                    f"{' ...' if len(weak_supplements) > 20 else ''}")
            if args.confirm_candidates:
                log(f"Checking {len(weak)} candidate(s)...")
                confirmed, unknown = discovery.probe_ids(client, weak, log=log)
                found |= set(confirmed)
                supplements |= {i for i in confirmed if idlib.is_supplement(i)}
                if unknown:
                    log(f"  {len(unknown)} candidate(s) were indeterminate.")
            else:
                log("  Pass --confirm-candidates to existence-check them "
                    f"(about {len(weak)} extra requests).")
        for error in catalog.errors[:10]:
            log(f"  ! {error}")

    bases = _resolve_probe_bases(args, found)
    if bases:
        suffixes = idlib.parse_suffix_spec(args.probe_suffixes)
        budget = discovery.probe_budget(len(bases), len(suffixes))
        minutes = budget * args.delay / 60
        log(f"\nProbe sweep: {len(bases)} base(s) x {len(suffixes)} suffix(es)")
        log(f"  up to {budget} requests, roughly {minutes:.0f} minute(s) at "
            f"{args.delay}s spacing.")
        if args.dry_run:
            log("  (dry run -- nothing fetched)")
        elif args.yes or _confirm("Proceed with the probe sweep?"):
            probed = discovery.probe_supplements(
                client, bases, suffixes,
                stop_after_misses=args.stop_after_misses, log=log,
            )
            found |= set(probed)
            supplements |= set(probed)
        else:
            log("  Skipped.")
    elif args.dry_run:
        log("\n(dry run -- no probe sweep configured)")

    discovered = idlib.sorted_ids(found)
    supplement_list = idlib.sorted_ids(supplements)
    if args.only_discovered:
        all_ids = discovered
    else:
        # Default to the full requested range: the catalog may be incomplete,
        # and an id that turns out not to exist costs exactly one request and
        # is then remembered as absent.
        all_ids = idlib.sorted_ids(found | set(idlib.base_range(args.first, args.last)))

    if not args.dry_run:
        _write_ids(args.out, all_ids, args)
        log(f"\nWrote {len(all_ids)} id(s) to {args.out}")
    log(f"  {len(discovered)} confirmed by the site, "
        f"{len(supplement_list)} supplement(s) found.")
    if supplement_list:
        log("  supplements: " + ", ".join(supplement_list))
    log(f"  {client.requests_made} request(s) made.")
    return 0


def _resolve_probe_bases(args, found: set) -> List[str]:
    spec = (args.probe_bases or "none").strip()
    if spec in ("", "none"):
        return []
    if spec == "all":
        return idlib.base_range(args.first, args.last)
    if spec == "found":
        return [i for i in idlib.sorted_ids(found) if not idlib.is_supplement(i)]
    if spec.startswith("@"):
        with open(spec[1:], "r", encoding="utf-8") as handle:
            return [i for i in idlib.iter_id_file(handle.read()) if not idlib.is_supplement(i)]
    bases = []
    for token in spec.replace(",", " ").split():
        normalized = idlib.normalize(token)
        if normalized is None:
            raise SystemExit(f"--probe-bases: {token!r} is not an issue id")
        bases.append(normalized)
    return bases


def _write_ids(path: str, all_ids: List[str], args) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(f"# 新清华 (XQH) issue ids, base range {args.first}-{args.last}\n")
        handle.write(f"# {len(all_ids)} ids; supplements are the ones with a letter marker\n")
        for issue_id in all_ids:
            handle.write(issue_id + "\n")


def cmd_download(client: PoliteClient, args, manifest_path: str, log) -> int:
    if args.ids:
        with open(args.ids, "r", encoding="utf-8") as handle:
            issue_ids = idlib.sorted_ids(idlib.iter_id_file(handle.read()))
        if not issue_ids:
            log(f"No usable ids in {args.ids}")
            return 1
    else:
        issue_ids = idlib.base_range(args.first, args.last)
        log(f"No --ids given; using the base range {args.first}-{args.last} "
            "(run 'discover' first to include supplements).")

    manifest = Manifest.load(manifest_path)
    if args.force:
        pending = list(issue_ids)
    else:
        pending = [i for i in issue_ids if not manifest.is_done(i, args.recheck_missing)]
    if args.limit is not None:
        pending = pending[: args.limit]

    log(f"{len(issue_ids)} id(s) requested, {len(pending)} to fetch, "
        f"{len(issue_ids) - len(pending)} already settled.")
    log(f"Pacing: one connection, ~{args.delay}s between requests "
        f"(~{len(pending) * args.delay / 3600:.1f}h of spacing for this run).")
    if args.dry_run:
        log("(dry run -- nothing fetched)")
        return 0
    if not pending:
        return 0

    stats = download_all(
        client, pending, args.dest, manifest,
        recheck_missing=args.recheck_missing, force=args.force, log=log,
    )
    log(
        f"\nDone: {stats['downloaded']} downloaded ({_mb(stats['bytes'])}), "
        f"{stats['missing']} absent, {stats['errors']} error(s), "
        f"{client.requests_made} request(s)."
    )
    if stats["errors"]:
        log("Rerun the same command to retry the errors.")
    if client.limiter.throttle_events:
        log(f"Server asked us to slow down {client.limiter.throttle_events} time(s); "
            f"final delay {client.limiter.delay:.1f}s. Consider a larger --delay next run.")
    return 0


def cmd_verify(args, manifest_path: str, log) -> int:
    manifest = Manifest.load(manifest_path)
    problems = verify_dir(args.dest, manifest)
    if not problems:
        log(f"All {sum(1 for _ in manifest.ids_with_status(OK))} recorded file(s) look good.")
        return 0
    log(f"{len(problems)} problem(s):")
    for issue_id, problem in problems:
        log(f"  {issue_id}: {problem}")
    # Read-only on purpose: verify reports, it does not delete. A file flagged
    # only for a missing %%EOF marker may still be the best copy available.
    retry_file = os.path.join(args.dest, "retry-ids.txt")
    with open(retry_file, "w", encoding="utf-8") as handle:
        handle.write("# ids that failed verification\n")
        for issue_id, _ in problems:
            handle.write(issue_id + "\n")
    log(f"\nWrote {len(problems)} id(s) to {retry_file}. Re-fetch them with:")
    log(f"  python -m thu_xqh download --ids {retry_file} --dest {args.dest} --force")
    return 1


def cmd_status(args, manifest_path: str, log) -> int:
    manifest = Manifest.load(manifest_path)
    if not manifest.entries:
        log(f"No manifest at {manifest_path} yet.")
        return 0
    counts = manifest.counts()
    total = sum(counts.values())
    downloaded = counts.get(OK, 0)
    size = sum(e.size for e in manifest.entries.values() if e.status == OK)
    supplements = [i for i in manifest.ids_with_status(OK) if idlib.is_supplement(i)]
    log(f"Manifest: {manifest_path}")
    log(f"  {total} id(s) attempted")
    log(f"  {downloaded} downloaded ({_mb(size)}), {len(supplements)} supplement(s)")
    log(f"  {counts.get(MISSING, 0)} absent, {counts.get(ERROR, 0)} error(s)")
    expected = args.last - args.first + 1
    log(f"  base range progress: {min(downloaded, expected)}/{expected}")
    if counts.get(ERROR):
        log("  retry with: python -m thu_xqh download --ids <ids file>")
    return 0


def _confirm(question: str) -> bool:
    if not sys.stdin.isatty():
        print(f"{question} [not a terminal -- assuming no; pass --yes to proceed]")
        return False
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")


def _mb(size: int) -> str:
    return f"{size / (1024 * 1024):.1f} MB"
