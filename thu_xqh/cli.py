"""Command line entry point."""

from __future__ import annotations

import argparse
import os
import sys
from typing import List, Optional

from . import collections as collib
from . import discover as discovery
from . import ids as idlib
from .client import BudgetExhausted, DEFAULT_USER_AGENT, PoliteClient, TransportError
from .collections import Collection
from .download import download_all, verify_dir
from .manifest import ERROR, MISSING, OK, Manifest

DEFAULT_BASE_URL = "https://thujournal.lib.tsinghua.edu.cn"
DEFAULT_DEST_ROOT = "pdfs"


def build_parser() -> argparse.ArgumentParser:
    # Shared options live on a parent parser so they can be written after the
    # subcommand, which is where people reach for them.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--base-url", default=DEFAULT_BASE_URL)
    common.add_argument(
        "--delay", type=float, default=5.0,
        help="seconds between requests, jittered (default: 5.0)",
    )
    common.add_argument("--timeout", type=float, default=45.0)
    common.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    common.add_argument(
        "--max-requests", type=int, default=None,
        help="stop after this many requests; rerun later to continue",
    )
    common.add_argument("--manifest", default=None, help="default: <dest>/manifest.json")
    common.add_argument(
        "--dest-root", default=DEFAULT_DEST_ROOT, metavar="DIR",
        help="parent directory; each collection gets its own subfolder inside "
             f"it (default: {DEFAULT_DEST_ROOT})",
    )
    common.add_argument(
        "--dest", default=None,
        help="exact download directory for this one collection, overriding "
             "--dest-root and its subfolder",
    )
    common.add_argument(
        "--ignore-robots", action="store_true",
        help="proceed even if robots.txt disallows the path",
    )
    common.add_argument("-q", "--quiet", action="store_true")

    # Which publication to work on.
    picker = argparse.ArgumentParser(add_help=False)
    picker.add_argument(
        "-c", "--collection", default="XQH", metavar="CODE",
        help="displayDBCode of the journal, e.g. XQH; 'all' runs every "
             "collection in the registry (default: XQH)",
    )
    picker.add_argument("--registry", default=collib.DEFAULT_REGISTRY)
    picker.add_argument("--sys-id", default=None, help="override sysId")
    picker.add_argument("--db-name", default=None, help="override displayDBName")
    picker.add_argument(
        "--pdf-dir", default=None,
        help="override the PDF directory (default: /swfPath/<code lowercased>)",
    )
    picker.add_argument("--id-width", type=int, default=None, help="digits in an issue id")
    picker.add_argument("--first", type=int, default=None)
    picker.add_argument("--last", type=int, default=None)
    picker.add_argument("--first-year", type=int, default=None)
    picker.add_argument("--last-year", type=int, default=None)

    parser = argparse.ArgumentParser(
        prog="python -m thu_xqh",
        description="Politely archive issue PDFs from the Tsinghua journal platform.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Typical run:\n"
            "  python -m thu_xqh collections\n"
            "  python -m thu_xqh discover -c XQH --out ids-xqh.txt\n"
            "  python -m thu_xqh download -c XQH --ids ids-xqh.txt\n"
            "  python -m thu_xqh verify -c XQH\n"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    listing = subparsers.add_parser(
        "collections", parents=[common], help="list the journals the platform offers"
    )
    listing.add_argument("--registry", default=collib.DEFAULT_REGISTRY)
    listing.add_argument(
        "--offline", action="store_true", help="show the cached registry, fetch nothing"
    )
    listing.add_argument(
        "--save", action="store_true", help="write what was found to the registry file"
    )

    find = subparsers.add_parser(
        "discover", parents=[common, picker],
        help="build the list of issue ids that exist",
    )
    find.add_argument("--out", default=None, help="default: issue-ids-<collection>.txt")
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
    find.add_argument("--coverage-target", type=float, default=0.98)
    find.add_argument(
        "--follow-details", action="store_true",
        help="open each detail page to read its PDF link; one request per issue, "
             "and the reliable route when index rows link only to detail pages",
    )
    find.add_argument(
        "--max-details", type=int, default=None,
        help="cap how many detail pages to open",
    )
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
        help="save fetched pages here, for checking what the parser saw",
    )
    find.add_argument("--dry-run", action="store_true", help="print the request budget and exit")
    find.add_argument("-y", "--yes", action="store_true", help="do not prompt before probing")

    get = subparsers.add_parser(
        "download", parents=[common, picker], help="fetch the PDFs"
    )
    get.add_argument("--ids", default=None, help="id list file (default: the base range)")
    get.add_argument("--limit", type=int, default=None, help="download at most N new files")
    get.add_argument("--recheck-missing", action="store_true")
    get.add_argument(
        "--force", action="store_true",
        help="re-fetch even ids the manifest already records as done",
    )
    get.add_argument("--dry-run", action="store_true")

    subparsers.add_parser(
        "verify", parents=[common, picker], help="re-check downloaded files on disk"
    )
    subparsers.add_parser(
        "status", parents=[common, picker], help="summarise manifest progress"
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    log = (lambda msg: None) if args.quiet else print

    if args.command == "collections":
        return cmd_collections(args, log)

    registry = collib.load_registry(args.registry)
    try:
        targets = _resolve_collections(args, registry, log)
    except SystemExit as exc:
        log(str(exc))
        return 2

    if args.command in ("status", "verify"):
        worst = 0
        for collection in targets:
            dest, manifest_path = _paths_for(args, collection)
            log(f"\n=== {collection.describe()} ===" if len(targets) > 1 else
                f"Collection: {collection.describe()}")
            if args.command == "status":
                code = cmd_status(args, collection, dest, manifest_path, log)
            else:
                code = cmd_verify(args, dest, manifest_path, log)
            worst = max(worst, code)
        return worst

    client = _make_client(args, log)
    worst = 0
    try:
        with client:
            for collection in targets:
                log(f"\n=== {collection.describe()} ===" if len(targets) > 1 else
                    f"Collection: {collection.describe()}")
                if not _robots_ok(client, collection, args, log):
                    return 2
                dest, manifest_path = _paths_for(args, collection)
                if args.command == "discover":
                    code = cmd_discover(client, collection, args, dest, log)
                else:
                    code = cmd_download(client, collection, args, dest, manifest_path, log)
                worst = max(worst, code)
        return worst
    except KeyboardInterrupt:
        log("\nInterrupted. Progress is saved -- rerun the same command to resume.")
        return 130
    except BudgetExhausted as exc:
        log(f"\nStopped: {exc}. Progress is saved -- rerun to continue.")
        return 0
    except TransportError as exc:
        log(f"\nStopped: {exc}")
        return 1


def _resolve_collections(args, registry, log) -> List[Collection]:
    """One collection, or every one in the registry for ``-c all``."""
    requested = (args.collection or "").strip()
    if requested.lower() != "all":
        collection = collib.resolve(
            requested, registry,
            sys_id=args.sys_id, name=args.db_name, pdf_dir=args.pdf_dir,
            id_width=args.id_width, first=args.first, last=args.last,
            first_year=args.first_year, last_year=args.last_year,
        )
        if requested.upper() not in registry:
            log(f"Note: {collection.code} is not in the registry; using defaults "
                f"derived from the code. Run 'collections' to discover its settings.")
        return [collection]

    # Per-collection outputs are the whole point of 'all'; a single explicit
    # path would have every journal overwrite the last one's work.
    for flag in ("dest", "manifest", "out", "ids"):  # note: --dest-root is fine
        if getattr(args, flag, None):
            raise SystemExit(f"--{flag} cannot be combined with '-c all'; "
                             f"run that collection on its own")
    for flag in ("sys_id", "db_name", "pdf_dir", "id_width", "first", "last"):
        if getattr(args, flag, None) is not None:
            raise SystemExit(f"--{flag.replace('_', '-')} cannot be combined with "
                             f"'-c all'; it only makes sense for one collection")
    if not registry:
        raise SystemExit("the registry is empty; run 'collections --save' first")
    return [registry[code] for code in sorted(registry)]


def _paths_for(args, collection: Collection):
    """Where this collection's files, manifest and id list live."""
    dest = args.dest or os.path.join(args.dest_root, collection.code.lower())
    manifest_path = args.manifest or os.path.join(dest, "manifest.json")
    return dest, manifest_path


def _make_client(args, log) -> PoliteClient:
    return PoliteClient(
        args.base_url,
        delay=args.delay,
        timeout=args.timeout,
        user_agent=args.user_agent,
        max_requests=args.max_requests,
        logger=log,
    )


def _robots_ok(client: PoliteClient, collection: Collection, args, log) -> bool:
    if args.ignore_robots:
        return True
    try:
        allowed = client.robots_allows(collection.pdf_path("0001"))
    except Exception as exc:
        log(f"Could not read robots.txt ({exc}); continuing.")
        return True
    if not allowed:
        log(
            f"robots.txt disallows {collection.pdf_dir}/ for this user agent.\n"
            "Refusing to crawl. Re-run with --ignore-robots only if you have "
            "permission from the site operator."
        )
    return allowed


def cmd_collections(args, log) -> int:
    registry = collib.load_registry(args.registry)
    if args.offline:
        log(f"{len(registry)} collection(s) in {args.registry}:")
        for collection in sorted(registry.values(), key=lambda c: c.code):
            log(f"  {collection.describe()}")
        return 0

    client = _make_client(args, log)
    log("Looking for collections on the platform...")
    try:
        with client:
            found = collib.discover_collections(client, log=log)
    except KeyboardInterrupt:
        log("\nInterrupted.")
        return 130
    except TransportError as exc:
        log(f"\nStopped: {exc}")
        return 1

    if not found:
        log("\nNo collections found in the entry pages. The links may be built by "
            "JavaScript, in which case pass the codes explicitly:")
        log("  python -m thu_xqh discover -c <CODE> --db-name <name>")
        return 1

    log(f"\n{len(found)} collection(s):")
    for collection in sorted(found.values(), key=lambda c: c.code):
        known = " (known)" if collection.code in collib.BUILTIN else ""
        log(f"  {collection.describe()}{known}")

    if args.save:
        merged = collib.load_registry(args.registry)
        for code, collection in found.items():
            if code in merged:
                # Keep the ranges we already know; take any new naming detail.
                if collection.name and not merged[code].name:
                    merged[code].name = collection.name
            else:
                merged[code] = collection
        collib.save_registry(merged, args.registry)
        log(f"\nSaved {len(merged)} collection(s) to {args.registry}")
    else:
        log("\nPass --save to record these in the registry.")
    log(f"\nNext: python -m thu_xqh discover -c <CODE> --max-level all "
        f"--coverage-target 0 --save-html debug/")
    return 0


def cmd_discover(
    client: PoliteClient, collection: Collection, args, dest: str, log
) -> int:
    found: set = set()
    supplements: set = set()
    detail_links: List[str] = []

    if not args.no_catalog:
        log("Walking the browse index...")
        catalog = discovery.catalog_scan(
            client, collection,
            coverage_target=args.coverage_target,
            max_level=args.max_level,
            save_html=args.save_html,
            log=log,
        )
        found |= catalog.confirmed
        supplements |= set(catalog.supplements())
        detail_links = sorted(catalog.detail_links)

        weak = idlib.sorted_ids(catalog.candidates)
        if weak:
            log(f"\n{len(weak)} id-shaped token(s) were not backed by a PDF link.")
            weak_supplements = catalog.candidate_supplements()
            if weak_supplements:
                log(f"  of which {len(weak_supplements)} look like supplements: "
                    f"{', '.join(weak_supplements[:20])}"
                    f"{' ...' if len(weak_supplements) > 20 else ''}")
            if args.confirm_candidates:
                log(f"Checking {len(weak)} candidate(s)...")
                confirmed, unknown = discovery.probe_ids(client, collection, weak, log=log)
                found |= set(confirmed)
                supplements |= {i for i in confirmed if idlib.is_supplement(i)}
                if unknown:
                    log(f"  {len(unknown)} candidate(s) were indeterminate.")
            else:
                log("  Pass --confirm-candidates to existence-check them "
                    f"(about {len(weak)} extra requests).")
        for error in catalog.errors[:10]:
            log(f"  ! {error}")

        if detail_links:
            log(f"\n{len(detail_links)} detail page(s) linked from the index.")
            if args.follow_details:
                targets = detail_links[: args.max_details] if args.max_details else detail_links
                log(f"Opening {len(targets)} detail page(s) -- one request each...")
                if not args.dry_run:
                    from_details = discovery.follow_details(
                        client, collection, targets, log=log, save_html=args.save_html
                    )
                    found |= from_details
                    supplements |= {i for i in from_details if idlib.is_supplement(i)}
                    log(f"  {len(from_details)} id(s) read from detail pages.")
            elif not found:
                log("  No PDF links were found on the index itself. Pass "
                    "--follow-details to read the ids from these pages "
                    f"(about {len(detail_links)} requests).")

    bases = _resolve_probe_bases(args, collection, found)
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
                client, collection, bases, suffixes,
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
    if args.only_discovered or not collection.has_known_range:
        all_ids = discovered
    else:
        # Default to the full known range: the catalog may be incomplete, and
        # an id that turns out not to exist costs one request and is then
        # remembered as absent.
        all_ids = idlib.sorted_ids(found | set(collection.issue_ids()))

    out_path = args.out or os.path.join(dest, f"issue-ids-{collection.code.lower()}.txt")
    if not args.dry_run:
        _write_ids(out_path, all_ids, collection)
        log(f"\nWrote {len(all_ids)} id(s) to {out_path}")
    log(f"  {len(discovered)} confirmed by the site, "
        f"{len(supplement_list)} supplement(s) found.")
    if supplement_list:
        log("  supplements: " + ", ".join(supplement_list[:50])
            + (" ..." if len(supplement_list) > 50 else ""))
    if not discovered and not args.no_catalog:
        log("  Nothing was confirmed. Re-run with --save-html debug/ and check "
            "what the pages actually contain.")
    log(f"  {client.requests_made} request(s) made.")
    return 0


def _resolve_probe_bases(args, collection: Collection, found: set) -> List[str]:
    spec = (args.probe_bases or "none").strip()
    if spec in ("", "none"):
        return []
    if spec == "all":
        if not collection.has_known_range:
            raise SystemExit(
                f"--probe-bases all needs a known issue range for {collection.code}; "
                "pass --first and --last"
            )
        return collection.issue_ids()
    if spec == "found":
        return [i for i in idlib.sorted_ids(found) if not idlib.is_supplement(i)]
    if spec.startswith("@"):
        with open(spec[1:], "r", encoding="utf-8") as handle:
            return [i for i in idlib.iter_id_file(handle.read()) if not idlib.is_supplement(i)]
    bases = []
    for token in spec.replace(",", " ").split():
        normalized = idlib.normalize_href_id(token)
        if normalized is None:
            raise SystemExit(f"--probe-bases: {token!r} is not an issue id")
        bases.append(normalized)
    return bases


def _write_ids(path: str, all_ids: List[str], collection: Collection) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(f"# {collection.code} ({collection.name}) issue ids\n")
        handle.write(f"# pdf directory: {collection.pdf_dir}\n")
        for series in collection.series():
            handle.write(f"# series: {series.label()} ({series.count} issues)\n")
        handle.write(f"# {len(all_ids)} ids; supplements carry a letter marker\n")
        for issue_id in all_ids:
            handle.write(issue_id + "\n")


def cmd_download(
    client: PoliteClient, collection: Collection, args, dest: str, manifest_path: str, log
) -> int:
    if args.ids:
        with open(args.ids, "r", encoding="utf-8") as handle:
            issue_ids = idlib.sorted_ids(idlib.iter_id_file(handle.read()))
        if not issue_ids:
            log(f"No usable ids in {args.ids}")
            return 1
    elif collection.has_known_range:
        issue_ids = collection.issue_ids()
        spans = " + ".join(s.label() for s in collection.series())
        log(f"No --ids given; using the known range {spans} "
            "(run 'discover' first to include supplements).")
    else:
        log(f"No --ids given and no known issue range for {collection.code}.\n"
            f"Run: python -m thu_xqh discover -c {collection.code} "
            f"--out ids-{collection.code.lower()}.txt")
        return 1

    manifest = Manifest.load(manifest_path)
    if args.force:
        pending = list(issue_ids)
    else:
        pending = [i for i in issue_ids if not manifest.is_done(i, args.recheck_missing)]
    if args.limit is not None:
        pending = pending[: args.limit]

    log(f"{len(issue_ids)} id(s) requested, {len(pending)} to fetch, "
        f"{len(issue_ids) - len(pending)} already settled.")
    log(f"Destination: {dest}")
    log(f"Pacing: one connection, ~{args.delay}s between requests "
        f"(~{len(pending) * args.delay / 3600:.1f}h of spacing for this run).")
    if args.dry_run:
        log("(dry run -- nothing fetched)")
        return 0
    if not pending:
        return 0

    stats = download_all(
        client, collection, pending, dest, manifest,
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


def cmd_verify(args, dest: str, manifest_path: str, log) -> int:
    manifest = Manifest.load(manifest_path)
    problems = verify_dir(dest, manifest)
    if not problems:
        log(f"All {sum(1 for _ in manifest.ids_with_status(OK))} recorded file(s) look good.")
        return 0
    log(f"{len(problems)} problem(s):")
    for issue_id, problem in problems:
        log(f"  {issue_id}: {problem}")
    # Read-only on purpose: verify reports, it does not delete. A file flagged
    # only for a missing %%EOF marker may still be the best copy available.
    retry_file = os.path.join(dest, "retry-ids.txt")
    with open(retry_file, "w", encoding="utf-8") as handle:
        handle.write("# ids that failed verification\n")
        for issue_id, _ in problems:
            handle.write(issue_id + "\n")
    log(f"\nWrote {len(problems)} id(s) to {retry_file}. Re-fetch them with:")
    log(f"  python -m thu_xqh download --ids {retry_file} --force")
    return 1


def cmd_status(args, collection: Collection, dest: str, manifest_path: str, log) -> int:
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
    if collection.has_known_range:
        expected = collection.expected_count()
        log(f"  known range progress: {min(downloaded, expected)}/{expected}")
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
