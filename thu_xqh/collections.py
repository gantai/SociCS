"""Collections (journals) hosted on the platform.

The archive holds several publications, selected by ``displayDBCode`` on the
browse pages and ``dbName`` on the detail pages -- ``XQH`` is 新清华, and the
PDF directory ``swfPath/xqh/`` is the same code lowercased.

A collection is shipped as a known-good entry only where its PDF directory and
issue range have actually been confirmed. Display names and sysIds are left
blank unless known: anything else is meant to be discovered from the site with
``python -m thu_xqh collections`` and cached in a registry file, rather than
guessed at here and quietly wrong.
"""

from __future__ import annotations

import json
import os
import re
import urllib.parse
from dataclasses import asdict, dataclass, field
from typing import Callable, Dict, List, Optional

from .client import PoliteClient, TransportError

HOME_PATHS = ("/", "/QHHome/Index")
INDEX_PATH = "/QHHome/SecondIndex"
FIRST_INDEX_PATH = "/QHHome/Index"

# The site spells it "Detali". Accept the correct spelling too, in case it is
# ever fixed.
DETAIL_RE = re.compile(r"/Deta(?:li|il)SwfInfo\?[^\s\"'<>]+", re.IGNORECASE)
HREF_RE = re.compile(r"""(?:href|src|action)\s*=\s*["']([^"']+)["']""", re.IGNORECASE)


@dataclass
class IdSeries:
    """A run of issue numbers sharing a filename prefix.

    Some collections keep a second series in the same directory -- 国立清华大学校刊
    has ``0001.pdf``..``0832.pdf`` alongside ``f0001.pdf``..``f0056.pdf``. They
    are one publication and one download directory, but two numberings.
    """

    prefix: str = ""
    first: int = 1
    last: int = 1
    width: int = 4

    @property
    def count(self) -> int:
        return max(0, self.last - self.first + 1)

    def ids(self) -> List[str]:
        return [
            f"{self.prefix}{str(n).zfill(self.width)}"
            for n in range(self.first, self.last + 1)
        ]

    def label(self) -> str:
        lo = f"{self.prefix}{str(self.first).zfill(self.width)}"
        hi = f"{self.prefix}{str(self.last).zfill(self.width)}"
        return f"{lo}-{hi}"


@dataclass
class Collection:
    """One publication on the platform."""

    code: str
    name: str = ""
    sys_id: str = ""
    pdf_dir: str = ""
    id_width: int = 4
    first: Optional[int] = None
    last: Optional[int] = None
    extra_series: List[IdSeries] = field(default_factory=list)
    first_year: int = 1911
    last_year: int = 2006

    def __post_init__(self):
        self.code = self.code.upper()
        if not self.pdf_dir:
            self.pdf_dir = f"/swfPath/{self.code.lower()}"
        self.pdf_dir = "/" + self.pdf_dir.strip("/")
        # Round-tripping through JSON turns the series back into plain dicts.
        self.extra_series = [
            s if isinstance(s, IdSeries) else IdSeries(**s) for s in self.extra_series
        ]

    @property
    def has_base_range(self) -> bool:
        """Whether the main, unprefixed numbering is known."""
        return self.first is not None and self.last is not None

    @property
    def has_known_range(self) -> bool:
        """Whether we can enumerate this collection's issues without asking."""
        return self.has_base_range or bool(self.extra_series)

    def series(self) -> List[IdSeries]:
        out: List[IdSeries] = []
        if self.has_base_range:
            out.append(IdSeries("", self.first, self.last, self.id_width))
        out.extend(self.extra_series)
        return out

    def issue_ids(self) -> List[str]:
        """Every id this collection is known to hold, across all its series."""
        seen: List[str] = []
        known = set()
        for series in self.series():
            for issue_id in series.ids():
                if issue_id not in known:
                    known.add(issue_id)
                    seen.append(issue_id)
        return seen

    def expected_count(self) -> int:
        return sum(s.count for s in self.series())

    def pdf_path(self, issue_id: str) -> str:
        return f"{self.pdf_dir}/{issue_id}.pdf"

    def describe(self) -> str:
        parts = [s.label() for s in self.series()]
        span = " + ".join(parts) if parts else "range unknown"
        total = f" [{self.expected_count()} files]" if parts else ""
        label = f"{self.code} ({self.name})" if self.name else self.code
        return f"{label}  sysId={self.sys_id or '?'}  pdf={self.pdf_dir}  {span}{total}"


# Collections whose PDF directory and issue range have been confirmed. Display
# names and sysIds are only filled in where they are actually known -- the rest
# are left blank for `collections` to discover, rather than guessed at here.
XQH = Collection(
    code="XQH",
    name="新清华",
    sys_id="23",
    pdf_dir="/swfPath/xqh",
    id_width=4,
    first=1,
    last=1670,
    first_year=1953,
    last_year=2006,
)

QHXK = Collection(
    code="QHXK",
    pdf_dir="/swfPath/qhxk",
    id_width=4,
    first=8,
    last=32,
    first_year=1911,
    last_year=1952,
)

QHXXXK = Collection(
    code="QHXXXK",
    pdf_dir="/swfPath/qhxxxk",
    id_width=4,
    first=1,
    last=36,
    first_year=1911,
    last_year=1952,
)

# Two series in one directory: the main run, plus an "f" series.
GLQHDXXK = Collection(
    code="GLQHDXXK",
    pdf_dir="/swfPath/glqhdxxk",
    id_width=4,
    first=1,
    last=832,
    extra_series=[IdSeries(prefix="f", first=1, last=56, width=4)],
    first_year=1928,
    last_year=1949,
)

RMQH = Collection(
    code="RMQH",
    pdf_dir="/swfPath/rmqh",
    id_width=4,
    first=1,
    last=24,
    first_year=1950,
    last_year=1952,
)

BUILTIN: Dict[str, Collection] = {
    c.code: c for c in (XQH, QHXK, QHXXXK, GLQHDXXK, RMQH)
}

ALL = "ALL"
"""``-c all`` -- every collection in the registry."""
DEFAULT_REGISTRY = "collections.json"


# -- registry ----------------------------------------------------------------


def load_registry(path: str = DEFAULT_REGISTRY) -> Dict[str, Collection]:
    """Built-in collections, overlaid with anything cached on disk."""
    registry = {code: Collection(**asdict(c)) for code, c in BUILTIN.items()}
    if not os.path.exists(path):
        return registry
    with open(path, "r", encoding="utf-8") as handle:
        try:
            raw = json.load(handle)
        except json.JSONDecodeError:
            return registry
    for entry in raw.get("collections", []):
        known = {k: v for k, v in entry.items() if k in Collection.__dataclass_fields__}
        if not known.get("code"):
            continue
        collection = Collection(**known)
        registry[collection.code] = collection
    return registry


def save_registry(collections: Dict[str, Collection], path: str = DEFAULT_REGISTRY) -> None:
    payload = {
        "collections": [asdict(c) for c in sorted(collections.values(), key=lambda c: c.code)]
    }
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)


def resolve(
    code: Optional[str],
    registry: Dict[str, Collection],
    *,
    sys_id: Optional[str] = None,
    name: Optional[str] = None,
    pdf_dir: Optional[str] = None,
    id_width: Optional[int] = None,
    first: Optional[int] = None,
    last: Optional[int] = None,
    first_year: Optional[int] = None,
    last_year: Optional[int] = None,
) -> Collection:
    """Pick a collection from the registry and apply any explicit overrides.

    An unknown code is not an error: it produces a collection with defaults
    derived from the code, so a newly discovered journal can be fetched without
    editing anything.
    """
    key = (code or XQH.code).upper()
    base = registry.get(key)
    fields = asdict(base) if base else {"code": key}
    overrides = {
        "sys_id": sys_id, "name": name, "pdf_dir": pdf_dir, "id_width": id_width,
        "first": first, "last": last, "first_year": first_year, "last_year": last_year,
    }
    for field_name, value in overrides.items():
        if value is not None:
            fields[field_name] = value
    return Collection(**fields)


# -- discovery ---------------------------------------------------------------


def collections_in_html(html: str, base_path: str = "/") -> Dict[str, Collection]:
    """Read collection codes out of the links on a page."""
    found: Dict[str, Collection] = {}
    for href in HREF_RE.findall(html):
        href = href.strip().replace("&amp;", "&")
        target = urllib.parse.urljoin(base_path, href)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(target).query)
        code = _first(query, "displayDBCode", "dbName", "dbcode")
        if not code or not re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,15}", code):
            continue
        name = _first(query, "displayDBName", "dbShowName") or ""
        sys_id = _first(query, "sysId", "sysid") or ""
        code = code.upper()
        existing = found.get(code)
        if existing is None:
            found[code] = Collection(code=code, name=name, sys_id=sys_id)
        else:
            # Later links may carry detail the first one lacked.
            if name and not existing.name:
                existing.name = name
            if sys_id and not existing.sys_id:
                existing.sys_id = sys_id
    return found


def _first(query: Dict[str, List[str]], *names: str) -> str:
    for name in names:
        values = query.get(name)
        if values and values[0].strip():
            return values[0].strip()
    return ""


def discover_collections(
    client: PoliteClient,
    *,
    extra_paths: Optional[List[str]] = None,
    log: Callable[[str], None] = print,
) -> Dict[str, Collection]:
    """Fetch the platform's entry pages and list the journals they link to."""
    found: Dict[str, Collection] = {}
    paths = list(HOME_PATHS) + list(extra_paths or [])
    for path in paths:
        try:
            response = client.request("GET", path)
        except TransportError as exc:
            log(f"  ! {path}: {exc}")
            continue
        if not response.ok:
            log(f"  ! {path}: HTTP {response.status}")
            continue
        page = collections_in_html(response.text(), path)
        for code, collection in page.items():
            if code in found:
                if collection.name and not found[code].name:
                    found[code].name = collection.name
            else:
                found[code] = collection
        log(f"  {path} -> {len(page)} collection(s)")
    return found
