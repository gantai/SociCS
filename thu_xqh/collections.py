"""Collections (journals) hosted on the platform.

The archive holds several publications, selected by ``displayDBCode`` on the
browse pages and ``dbName`` on the detail pages -- ``XQH`` is 新清华, and the
PDF directory ``swfPath/xqh/`` is the same code lowercased.

Only XQH is shipped as a known-good entry, because it is the only one whose
URLs have been seen. The rest are meant to be discovered from the site with
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
class Collection:
    """One publication on the platform."""

    code: str
    name: str = ""
    sys_id: str = "23"
    pdf_dir: str = ""
    id_width: int = 4
    first: Optional[int] = None
    last: Optional[int] = None
    first_year: int = 1911
    last_year: int = 2006

    def __post_init__(self):
        self.code = self.code.upper()
        if not self.pdf_dir:
            self.pdf_dir = f"/swfPath/{self.code.lower()}"
        self.pdf_dir = "/" + self.pdf_dir.strip("/")

    @property
    def has_known_range(self) -> bool:
        return self.first is not None and self.last is not None

    def pdf_path(self, issue_id: str) -> str:
        return f"{self.pdf_dir}/{issue_id}.pdf"

    def describe(self) -> str:
        span = (f"{self.first}-{self.last}" if self.has_known_range else "range unknown")
        label = f"{self.code} ({self.name})" if self.name else self.code
        return f"{label}  sysId={self.sys_id}  pdf={self.pdf_dir}  {span}"


# The one collection whose URLs are confirmed, from the task description.
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

BUILTIN: Dict[str, Collection] = {XQH.code: XQH}
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
            found[code] = Collection(code=code, name=name, sys_id=sys_id or "23")
        else:
            # Later links may carry detail the first one lacked.
            if name and not existing.name:
                existing.name = name
            if sys_id and existing.sys_id == "23":
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
