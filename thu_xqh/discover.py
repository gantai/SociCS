"""Finding out which issues exist -- especially the supplements.

The naive way to find supplements like ``1670Z22`` is to guess: append every
plausible marker to every one of the 1670 base numbers and see what sticks.
That is tens of thousands of requests to a small library server for a handful
of hits, and it still only finds the markers you thought to guess.

So we ask the site instead. The archive publishes a browse index:

    /QHHome/SecondIndex?sysId=23&displayDBCode=XQH&displayDBName=新清华
                       &displayyear=1955&displaymonth=11

``displayDBCode=XQH`` is the same collection as the ``swfPath/xqh/`` PDF
directory, and ``displayyear`` accepts 全部 ("all"). Walking that index lists
every issue the archive knows about, supplements included, in a few dozen
requests -- and it finds markers we would never have guessed.

Probing is kept as a fallback for when the catalog is unreachable or looks
incomplete. It is opt-in, scoped, and always costed out before it runs.
"""

from __future__ import annotations

import os
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from . import ids as idlib
from .client import PoliteClient, TransportError
from .download import probe

ALL = "全部"
INDEX_PATH = "/QHHome/SecondIndex"
DEFAULT_SYS_ID = "23"
DEFAULT_DB_CODE = "XQH"
DEFAULT_DB_NAME = "新清华"
DEFAULT_FIRST_YEAR = 1953
DEFAULT_LAST_YEAR = 2006

# Ids taken straight out of a PDF href -- these are facts, not inferences.
PDF_HREF_RE = re.compile(r"/swfPath/xqh/([0-9A-Za-z]{4,12})\.pdf", re.IGNORECASE)
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


@dataclass
class Catalog:
    """What a catalog walk turned up."""

    confirmed: Set[str] = field(default_factory=set)
    """Ids read from an actual ``swfPath/xqh/....pdf`` link."""

    candidates: Set[str] = field(default_factory=set)
    """Id-shaped tokens found elsewhere on the page. Plausible, not proven."""

    pages_fetched: int = 0
    level: str = ""
    levels_tried: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    @property
    def all_ids(self) -> Set[str]:
        return self.confirmed | self.candidates

    def coverage(self, first: int, last: int) -> float:
        """Fraction of the expected base-issue range this walk accounted for."""
        expected = last - first + 1
        if expected <= 0:
            return 0.0
        seen = {idlib.split_id(i)[0] for i in self.all_ids}
        return len({b for b in seen if first <= b <= last}) / expected

    def supplements(self) -> List[str]:
        """Supplements we actually saw a PDF link for."""
        return idlib.sorted_ids(i for i in self.confirmed if idlib.is_supplement(i))

    def candidate_supplements(self) -> List[str]:
        """Supplement-shaped ids that still need confirming."""
        return idlib.sorted_ids(i for i in self.candidates if idlib.is_supplement(i))


def index_url(
    year: str,
    month: str,
    *,
    sys_id: str = DEFAULT_SYS_ID,
    db_code: str = DEFAULT_DB_CODE,
    db_name: str = DEFAULT_DB_NAME,
    extra: Optional[Dict[str, str]] = None,
) -> str:
    params = {
        "sysId": sys_id,
        "displayDBCode": db_code,
        "displayDBName": db_name,
        "displayyear": year,
        "displaymonth": month,
    }
    if extra:
        params.update(extra)
    return f"{INDEX_PATH}?{urllib.parse.urlencode(params)}"


def extract_ids(html: str, first: int, last: int) -> Tuple[Set[str], Set[str]]:
    """Pull issue ids out of an index page.

    Returns ``(confirmed, candidates)``. Anything sitting in a PDF href is
    confirmed. Candidates come only from link-carrying attributes -- a detail
    page URL like ``?id=1670Z22`` is a real lead, whereas a bare four-digit
    number in the page text is as likely to be a year or a row count.
    """
    confirmed: Set[str] = set()
    for raw in PDF_HREF_RE.findall(html):
        normalized = idlib.normalize(raw)
        if normalized and idlib.in_range(normalized, first, last):
            confirmed.add(normalized)

    candidates: Set[str] = set()
    for value in LINKY_ATTR_RE.findall(html):
        if "/swfPath/" in value:
            continue  # already handled, and its ids are confirmed
        for raw in idlib.ID_IN_TEXT_RE.findall(value):
            normalized = idlib.normalize(raw)
            if not normalized or normalized in confirmed:
                continue
            if not idlib.in_range(normalized, first, last):
                continue
            candidates.add(normalized)
    return confirmed, candidates


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
    year: str,
    month: str,
    *,
    first: int,
    last: int,
    max_pages: int = 40,
    log: Callable[[str], None] = lambda msg: None,
    save_html: Optional[str] = None,
    **url_kwargs,
) -> Catalog:
    """Fetch one index view, following its pagination links."""
    result = Catalog()
    queue = [index_url(year, month, **url_kwargs)]
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
        confirmed, candidates = extract_ids(html, first, last)
        before = len(result.all_ids)
        result.confirmed |= confirmed
        result.candidates |= candidates
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
    *,
    first: int = idlib.DEFAULT_FIRST,
    last: int = idlib.DEFAULT_LAST,
    first_year: int = DEFAULT_FIRST_YEAR,
    last_year: int = DEFAULT_LAST_YEAR,
    coverage_target: float = 0.98,
    max_level: str = "year-month",
    log: Callable[[str], None] = print,
    save_html: Optional[str] = None,
    **url_kwargs,
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
        views = _views_for_level(level, first_year, last_year)
        log(f"  catalog level '{level}': {len(views)} index view(s)")
        merged = Catalog(level=level)
        for year, month in views:
            partial = scan_index(
                client, year, month, first=first, last=last, log=log,
                save_html=save_html, **url_kwargs
            )
            merged.confirmed |= partial.confirmed
            merged.candidates |= partial.candidates
            merged.pages_fetched += partial.pages_fetched
            merged.errors.extend(partial.errors)
        merged.candidates -= merged.confirmed

        coverage = merged.coverage(first, last)
        log(f"  -> {len(merged.confirmed)} confirmed, {len(merged.candidates)} candidate, "
            f"{len(merged.supplements())} supplement(s), coverage {coverage:.1%}")
        if len(merged.all_ids) > len(best.all_ids):
            best = merged
        if coverage >= coverage_target:
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


# -- fallback: guessing ------------------------------------------------------


def probe_ids(
    client: PoliteClient,
    issue_ids: Sequence[str],
    *,
    log: Callable[[str], None] = print,
) -> Tuple[List[str], List[str]]:
    """Existence-check a list of ids. Returns ``(found, unknown)``."""
    found: List[str] = []
    unknown: List[str] = []
    for index, issue_id in enumerate(issue_ids, 1):
        verdict = probe(client, issue_id)
        if verdict is True:
            found.append(issue_id)
            log(f"  [{index}/{len(issue_ids)}] {issue_id}  found")
        elif verdict is None:
            unknown.append(issue_id)
            log(f"  [{index}/{len(issue_ids)}] {issue_id}  indeterminate")
    return found, unknown


def probe_supplements(
    client: PoliteClient,
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
            candidate = idlib.normalize(f"{base}{suffix}")
            if candidate is None:
                log(f"  ! skipping malformed candidate {base}{suffix}")
                continue
            verdict = probe(client, candidate)
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
    """Save a fetched index page so its markup can be inspected offline."""
    os.makedirs(directory, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9]+", "_", path).strip("_")[:120] or "index"
    with open(os.path.join(directory, safe + ".html"), "w", encoding="utf-8") as handle:
        handle.write(html)


def probe_budget(bases: int, suffixes: int) -> int:
    """Worst-case request count for a probe sweep (HEAD, plus a ranged GET)."""
    return bases * suffixes * 2
