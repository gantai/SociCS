"""Finding out which issues exist -- especially the supplements.

The naive way to find supplements like ``1670Z22`` is to guess: append every
plausible marker to every one of the base numbers and see what sticks. That is
tens of thousands of requests to a small library server for a handful of hits,
and it still only finds the markers you thought to guess.

So we ask the site instead. Each collection publishes a browse index:

    /QHHome/SecondIndex?sysId=23&displayDBCode=XQH&displayDBName=新清华
                       &displayyear=1955&displaymonth=11

``displayDBCode`` selects the publication and matches the PDF directory
(``XQH`` -> ``swfPath/xqh/``), and ``displayyear`` accepts 全部 ("all").

Index rows may link straight to a PDF, or only to a detail page
(``/DetaliSwfInfo?dbName=XQH&sysID=135717``) whose own markup carries the PDF
link. Both routes are supported: the cheap one is tried first, and following
detail pages is opt-in because it costs one request per issue.
"""

from __future__ import annotations

import os
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

from . import ids as idlib
from .collections import DETAIL_RE, INDEX_PATH, Collection
from .client import PoliteClient, TransportError
from .download import probe

ALL = "全部"
DEFAULT_FIRST_YEAR = 1953
DEFAULT_LAST_YEAR = 2006

HREF_RE = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
PAGE_PARAM_RE = re.compile(r"(?:page|pageIndex|pageNo|pageNum|curPage)=(\d+)", re.IGNORECASE)

# Attributes that can plausibly carry an issue id. Deliberately excludes `id`
# and `class`: a page is full of four-digit element ids and stray numbers, and
# treating those as issues produces confident nonsense.
LINKY_ATTR_RE = re.compile(
    r"""\b(?:href|src|action|onclick|data-[a-z-]*(?:id|url|path|file|pdf)[a-z-]*)"""
    r"""\s*=\s*["']([^"']*)["']""",
    re.IGNORECASE,
)


def pdf_href_re(collection: Collection) -> "re.Pattern[str]":
    """Match PDF links belonging to this collection's directory."""
    return re.compile(
        re.escape(collection.pdf_dir) + r"/([0-9A-Za-z]{1,16})\.pdf", re.IGNORECASE
    )


@dataclass
class Catalog:
    """What a catalog walk turned up."""

    confirmed: Set[str] = field(default_factory=set)
    """Ids read from an actual ``….pdf`` link."""

    candidates: Set[str] = field(default_factory=set)
    """Id-shaped tokens found elsewhere on the page. Plausible, not proven."""

    detail_links: Set[str] = field(default_factory=set)
    """Detail-page paths seen, for the optional second pass."""

    pages_fetched: int = 0
    level: str = ""
    levels_tried: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    @property
    def all_ids(self) -> Set[str]:
        return self.confirmed | self.candidates

    def coverage(self, first: Optional[int], last: Optional[int]) -> float:
        """Fraction of the expected issue range this walk accounted for.

        Meaningless without a known range, so an unknown range reports full
        coverage rather than driving a pointless escalation to 648 requests.
        """
        if first is None or last is None:
            return 1.0
        expected = last - first + 1
        if expected <= 0:
            return 0.0
        seen = {idlib.split_id(i)[0] for i in self.all_ids if _numeric(i)}
        return len({b for b in seen if first <= b <= last}) / expected

    def supplements(self) -> List[str]:
        """Supplements we actually saw a PDF link for."""
        return idlib.sorted_ids(i for i in self.confirmed if _is_supplement(i))

    def candidate_supplements(self) -> List[str]:
        """Supplement-shaped ids that still need confirming."""
        return idlib.sorted_ids(i for i in self.candidates if _is_supplement(i))

    def merge(self, other: "Catalog") -> None:
        self.confirmed |= other.confirmed
        self.candidates |= other.candidates
        self.detail_links |= other.detail_links
        self.pages_fetched += other.pages_fetched
        self.errors.extend(other.errors)


def _numeric(issue_id: str) -> bool:
    try:
        idlib.split_id(issue_id)
        return True
    except ValueError:
        return False


def _is_supplement(issue_id: str) -> bool:
    try:
        return idlib.is_supplement(issue_id)
    except ValueError:
        return False


def index_url(
    collection: Collection,
    year: str,
    month: str,
    *,
    extra: Optional[Dict[str, str]] = None,
) -> str:
    params = {
        "sysId": collection.sys_id,
        "displayDBCode": collection.code,
        "displayDBName": collection.name,
        "displayyear": year,
        "displaymonth": month,
    }
    if extra:
        params.update(extra)
    # Send only what we actually know: an empty sysId or display name is a gap
    # in our knowledge, not a value the site should be asked to match.
    params = {k: v for k, v in params.items() if v not in (None, "")}
    return f"{INDEX_PATH}?{urllib.parse.urlencode(params)}"


def extract_ids(html: str, collection: Collection) -> Tuple[Set[str], Set[str]]:
    """Pull issue ids out of an index page.

    Returns ``(confirmed, candidates)``. Anything sitting in a PDF href for
    this collection is confirmed. Candidates come only from link-carrying
    attributes -- a detail page URL like ``?id=1670Z22`` is a real lead,
    whereas a bare four-digit number in the page text is as likely to be a year
    or a row count.
    """
    confirmed: Set[str] = set()
    for raw in pdf_href_re(collection).findall(html):
        normalized = idlib.normalize_href_id(raw)
        if normalized:
            confirmed.add(normalized)

    candidates: Set[str] = set()
    for value in LINKY_ATTR_RE.findall(html):
        if collection.pdf_dir.lower() in value.lower():
            continue  # already handled, and its ids are confirmed
        for raw in idlib.ID_IN_TEXT_RE.findall(value):
            normalized = idlib.normalize(raw, collection.id_width)
            if not normalized or normalized in confirmed:
                continue
            if not idlib.in_range(normalized, collection.first, collection.last):
                continue
            candidates.add(normalized)
    return confirmed, candidates


def extract_detail_links(html: str, base_path: str) -> Set[str]:
    """Detail-page paths (``/DetaliSwfInfo?dbName=…&sysID=…``) on a page."""
    links: Set[str] = set()
    for match in DETAIL_RE.findall(html):
        target = urllib.parse.urljoin(base_path, match.replace("&amp;", "&"))
        parts = urllib.parse.urlsplit(target)
        links.add(parts.path + (f"?{parts.query}" if parts.query else ""))
    return links


def _pagination_paths(html: str, current_path: str) -> List[str]:
    """Same-index links that differ only by a page number."""
    base_page = PAGE_PARAM_RE.search(current_path)
    seen: List[str] = []
    for href in HREF_RE.findall(html):
        href = href.strip().replace("&amp;", "&")
        if INDEX_PATH.lower() not in href.lower():
            continue
        if not PAGE_PARAM_RE.search(href):
            continue
        target = urllib.parse.urljoin(current_path, href)
        parts = urllib.parse.urlsplit(target)
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        if path == current_path or path in seen:
            continue
        if base_page and PAGE_PARAM_RE.search(path).group(1) == base_page.group(1):
            continue
        seen.append(path)
    return seen


def scan_index(
    client: PoliteClient,
    collection: Collection,
    year: str,
    month: str,
    *,
    max_pages: int = 40,
    log: Callable[[str], None] = lambda msg: None,
    save_html: Optional[str] = None,
) -> Catalog:
    """Fetch one index view, following its pagination links."""
    result = Catalog()
    queue = [index_url(collection, year, month)]
    visited: Set[str] = set()

    while queue and result.pages_fetched < max_pages:
        path = queue.pop(0)
        if path in visited:
            continue
        visited.add(path)
        try:
            response = client.request("GET", path)
        except TransportError as exc:
            result.errors.append(f"{path}: {exc}")
            continue
        result.pages_fetched += 1
        if not response.ok:
            result.errors.append(f"{path}: HTTP {response.status}")
            continue
        html = response.text()
        if save_html:
            _dump(save_html, path, html)
        confirmed, candidates = extract_ids(html, collection)
        before = len(result.all_ids)
        result.confirmed |= confirmed
        result.candidates |= candidates
        result.detail_links |= extract_detail_links(html, path)
        gained = len(result.all_ids) - before
        for next_path in _pagination_paths(html, path):
            if next_path not in visited:
                queue.append(next_path)
        log(f"    {path[:96]} -> {len(confirmed)} confirmed, {len(candidates)} candidate, "
            f"+{gained} new")
    if queue:
        result.errors.append(
            f"stopped at max_pages={max_pages} with {len(queue)} page(s) unvisited"
        )
    return result


def catalog_scan(
    client: PoliteClient,
    collection: Collection,
    *,
    coverage_target: float = 0.98,
    max_level: str = "year-month",
    log: Callable[[str], None] = print,
    save_html: Optional[str] = None,
) -> Catalog:
    """Walk the browse index, widening only as far as needed.

    Starts with the single cheapest query (all years, all months) and only
    escalates to finer-grained views when the result does not account for
    enough of the expected issue range. Most of the time this costs a handful
    of requests; the exhaustive year x month walk is the last resort.
    """
    levels = ["all", "month", "year", "year-month"]
    if max_level not in levels:
        raise ValueError(f"max_level must be one of {levels}")
    allowed = levels[: levels.index(max_level) + 1]

    best = Catalog()
    tried: List[str] = []
    for level in allowed:
        tried.append(level)
        views = _views_for_level(level, collection.first_year, collection.last_year)
        log(f"  catalog level '{level}': {len(views)} index view(s)")
        merged = Catalog(level=level)
        for year, month in views:
            merged.merge(
                scan_index(client, collection, year, month, log=log, save_html=save_html)
            )
        merged.candidates -= merged.confirmed

        coverage = merged.coverage(collection.first, collection.last)
        log(f"  -> {len(merged.confirmed)} confirmed, {len(merged.candidates)} candidate, "
            f"{len(merged.supplements())} supplement(s), {len(merged.detail_links)} "
            f"detail link(s), coverage {coverage:.1%}")
        if len(merged.all_ids) > len(best.all_ids):
            best = merged
        if coverage >= coverage_target and merged.all_ids:
            merged.levels_tried = tried
            return merged
    best.levels_tried = tried
    return best


def _views_for_level(level: str, first_year: int, last_year: int) -> List[Tuple[str, str]]:
    months = [str(m).zfill(2) for m in range(1, 13)]
    years = [str(y) for y in range(first_year, last_year + 1)]
    if level == "all":
        return [(ALL, ALL)]
    if level == "month":
        return [(ALL, m) for m in months]
    if level == "year":
        return [(y, ALL) for y in years]
    return [(y, m) for y in years for m in months]


def follow_details(
    client: PoliteClient,
    collection: Collection,
    detail_paths: Sequence[str],
    *,
    log: Callable[[str], None] = print,
    save_html: Optional[str] = None,
) -> Set[str]:
    """Open each detail page and read the PDF id out of it.

    One request per issue, which is why this is opt-in. It is the reliable
    route when index rows link only to detail pages.
    """
    found: Set[str] = set()
    pattern = pdf_href_re(collection)
    for index, path in enumerate(detail_paths, 1):
        try:
            response = client.request("GET", path)
        except TransportError as exc:
            log(f"  ! {path}: {exc}")
            continue
        if not response.ok:
            log(f"  ! {path}: HTTP {response.status}")
            continue
        html = response.text()
        if save_html:
            _dump(save_html, path, html)
        hits = {idlib.normalize_href_id(r) for r in pattern.findall(html)}
        hits.discard(None)
        found |= hits
        log(f"  [{index}/{len(detail_paths)}] {path[:72]} -> {sorted(hits) or 'nothing'}")
    return found


# -- fallback: guessing ------------------------------------------------------


def probe_ids(
    client: PoliteClient,
    collection: Collection,
    issue_ids: Sequence[str],
    *,
    log: Callable[[str], None] = print,
) -> Tuple[List[str], List[str]]:
    """Existence-check a list of ids. Returns ``(found, unknown)``."""
    found: List[str] = []
    unknown: List[str] = []
    for index, issue_id in enumerate(issue_ids, 1):
        verdict = probe(client, collection, issue_id)
        if verdict is True:
            found.append(issue_id)
            log(f"  [{index}/{len(issue_ids)}] {issue_id}  found")
        elif verdict is None:
            unknown.append(issue_id)
            log(f"  [{index}/{len(issue_ids)}] {issue_id}  indeterminate")
    return found, unknown


def probe_supplements(
    client: PoliteClient,
    collection: Collection,
    bases: Sequence[str],
    suffixes: Sequence[str],
    *,
    stop_after_misses: int = 0,
    log: Callable[[str], None] = print,
) -> List[str]:
    """Guess supplement ids for the given base issues.

    ``stop_after_misses`` gives up on a base once that many suffixes in a row
    come back empty, which keeps the request count down when a base simply has
    no supplements -- the common case.
    """
    found: List[str] = []
    for base in bases:
        misses = 0
        for suffix in suffixes:
            candidate = idlib.normalize_href_id(f"{base}{suffix}")
            if candidate is None:
                log(f"  ! skipping malformed candidate {base}{suffix}")
                continue
            verdict = probe(client, collection, candidate)
            if verdict is True:
                found.append(candidate)
                misses = 0
                log(f"  {candidate}  found")
            else:
                misses += 1
                if stop_after_misses and misses >= stop_after_misses:
                    break
    return found


def _dump(directory: str, path: str, html: str) -> None:
    """Save a fetched page so its markup can be inspected offline."""
    os.makedirs(directory, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9]+", "_", path).strip("_")[:120] or "index"
    with open(os.path.join(directory, safe + ".html"), "w", encoding="utf-8") as handle:
        handle.write(html)


def probe_budget(bases: int, suffixes: int) -> int:
    """Worst-case request count for a probe sweep (HEAD, plus a ranged GET)."""
    return bases * suffixes * 2
