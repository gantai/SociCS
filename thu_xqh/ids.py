"""Issue identifiers.

A regular issue is a zero-padded four-digit number (``0001`` .. ``1670``).
A supplement hangs off a base issue and carries a trailing marker, e.g.
``1670Z22``. Everything downstream treats an id as an opaque string; this
module is the only place that knows how one is shaped.
"""

from __future__ import annotations

import re
from typing import Iterable, Iterator, List, Optional, Tuple

BASE_WIDTH = 4
DEFAULT_FIRST = 1
DEFAULT_LAST = 1670

# 4 digits, optionally followed by a letter marker and up to three digits.
ID_RE = re.compile(r"^(\d{4})([A-Z]\d{0,3})?$")

# The same idea without a fixed width, for collections whose numbering we have
# not seen, and for ids read straight out of a PDF link.
GENERAL_ID_RE = re.compile(r"^(\d+)([A-Z][A-Z0-9]{0,4})?$")

# Used when scraping: the same shape, but anchored on word boundaries so we can
# pull ids out of surrounding HTML/JS without dragging in neighbouring text.
ID_IN_TEXT_RE = re.compile(r"(?<![0-9A-Za-z])(\d{4}(?:[A-Za-z]\d{0,3})?)(?![0-9A-Za-z])")


def format_base(number: int, width: int = BASE_WIDTH) -> str:
    return str(number).zfill(width)


def base_range(
    first: int = DEFAULT_FIRST, last: int = DEFAULT_LAST, width: int = BASE_WIDTH
) -> List[str]:
    if first < 1 or last < first:
        raise ValueError(f"invalid range {first}..{last}")
    return [format_base(n, width) for n in range(first, last + 1)]


def normalize(raw: str, width: int = BASE_WIDTH) -> Optional[str]:
    """Canonicalise an id of a known width, or return None if it is not one.

    Uppercases the supplement marker so ``1670z22`` and ``1670Z22`` collapse to
    a single identity.
    """
    candidate = raw.strip().upper()
    if width == BASE_WIDTH:
        return candidate if ID_RE.match(candidate) else None
    match = GENERAL_ID_RE.match(candidate)
    if not match or len(match.group(1)) != width:
        return None
    return candidate


def normalize_href_id(raw: str) -> Optional[str]:
    """Canonicalise an id read from an actual PDF link.

    Deliberately lenient about width: this came from a real ``….pdf`` href, so
    it is an issue whether or not it matches the numbering we expected.
    """
    candidate = raw.strip().upper()
    if not GENERAL_ID_RE.match(candidate) or len(candidate) > 16:
        return None
    return candidate


def split_id(issue_id: str) -> Tuple[int, str]:
    """``"1670Z22"`` -> ``(1670, "Z22")``; ``"0001"`` -> ``(1, "")``."""
    match = GENERAL_ID_RE.match(issue_id.upper())
    if not match:
        raise ValueError(f"not an issue id: {issue_id!r}")
    return int(match.group(1)), match.group(2) or ""


def is_supplement(issue_id: str) -> bool:
    return bool(split_id(issue_id)[1])


def in_range(
    issue_id: str,
    first: Optional[int] = DEFAULT_FIRST,
    last: Optional[int] = DEFAULT_LAST,
) -> bool:
    """Whether the id's base number falls inside the known issue range.

    The scrape regex is intentionally loose, so this is what keeps stray
    four-digit tokens (years, counts, element ids) out of the work list. A
    collection with no known range accepts anything id-shaped -- there is
    nothing to check it against.
    """
    try:
        base, _ = split_id(issue_id)
    except ValueError:
        return False
    if first is not None and base < first:
        return False
    if last is not None and base > last:
        return False
    return True


def sort_key(issue_id: str) -> Tuple[int, str]:
    try:
        base, suffix = split_id(issue_id)
    except ValueError:
        return 10 ** 12, issue_id
    return base, suffix


def sorted_ids(ids: Iterable[str]) -> List[str]:
    return sorted(set(ids), key=sort_key)


_BRACE_RE = re.compile(r"^(?P<prefix>[A-Za-z]*)\{(?P<lo>\d+)\.\.(?P<hi>\d+)\}$")


def parse_suffix_spec(spec: str) -> List[str]:
    """Expand a suffix spec into concrete markers.

    ``"Z{1..3},S1"`` -> ``["Z1", "Z2", "Z3", "S1"]``. Ranges keep the padding of
    the lower bound, so ``Z{01..03}`` yields ``Z01, Z02, Z03``.
    """
    out: List[str] = []
    for piece in spec.split(","):
        piece = piece.strip()
        if not piece:
            continue
        match = _BRACE_RE.match(piece)
        if not match:
            out.append(piece.upper())
            continue
        prefix = match.group("prefix").upper()
        lo_raw, hi_raw = match.group("lo"), match.group("hi")
        lo, hi = int(lo_raw), int(hi_raw)
        if hi < lo:
            raise ValueError(f"descending range in suffix spec: {piece!r}")
        width = len(lo_raw) if lo_raw.startswith("0") else 0
        for n in range(lo, hi + 1):
            out.append(f"{prefix}{str(n).zfill(width)}")
    seen = set()
    unique = []
    for suffix in out:
        if suffix not in seen:
            seen.add(suffix)
            unique.append(suffix)
    return unique


def iter_id_file(text: str) -> Iterator[str]:
    """Read ids from a file body, ignoring blanks and ``#`` comments."""
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        for token in line.replace(",", " ").split():
            normalized = normalize_href_id(token)
            if normalized:
                yield normalized
