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

# The full shape of an id: an optional letter prefix (``f0001`` -- a separate
# series sharing a collection's directory), the issue number, and an optional
# supplement marker (``1670Z22``).
#
# Case is preserved throughout. An earlier version uppercased ids to collapse
# duplicates, which is wrong the moment a real path is lowercase: ``f0001.pdf``
# is not ``F0001.pdf`` to a case-sensitive server.
FULL_ID_RE = re.compile(
    r"^(?P<prefix>[A-Za-z]{0,3})(?P<num>\d{1,8})(?P<suffix>[A-Za-z]\d{0,3})?$"
)

# Used when scraping: the same shape, but anchored on word boundaries so we can
# pull ids out of surrounding HTML/JS without dragging in neighbouring text.
ID_IN_TEXT_RE = re.compile(r"(?<![0-9A-Za-z])(\d{4}(?:[A-Za-z]\d{0,3})?)(?![0-9A-Za-z])")


def format_base(number: int, width: int = BASE_WIDTH, prefix: str = "") -> str:
    return f"{prefix}{str(number).zfill(width)}"


def base_range(
    first: int = DEFAULT_FIRST,
    last: int = DEFAULT_LAST,
    width: int = BASE_WIDTH,
    prefix: str = "",
) -> List[str]:
    if first < 1 or last < first:
        raise ValueError(f"invalid range {first}..{last}")
    return [format_base(n, width, prefix) for n in range(first, last + 1)]


def normalize(raw: str, width: int = BASE_WIDTH) -> Optional[str]:
    """Canonicalise a plain issue id of a known width, or None if it is not one.

    Rejects prefixed ids: this sifts loose page text, where a token like
    ``f0001`` is far more likely to be markup than an issue.
    """
    candidate = raw.strip()
    match = FULL_ID_RE.match(candidate)
    if not match or match.group("prefix"):
        return None
    if len(match.group("num")) != width:
        return None
    return candidate


def normalize_href_id(raw: str) -> Optional[str]:
    """Canonicalise an id read from an actual PDF link or an id file.

    Lenient about width and prefix: this came from a real ``….pdf`` path, so it
    is an issue whether or not it matches the numbering we expected.
    """
    candidate = raw.strip()
    if not candidate or len(candidate) > 16 or not FULL_ID_RE.match(candidate):
        return None
    return candidate


def parse_id(issue_id: str) -> Tuple[str, int, str]:
    """``"f0001"`` -> ``("f", 1, "")``; ``"1670Z22"`` -> ``("", 1670, "Z22")``."""
    match = FULL_ID_RE.match(issue_id.strip())
    if not match:
        raise ValueError(f"not an issue id: {issue_id!r}")
    return match.group("prefix"), int(match.group("num")), match.group("suffix") or ""


def split_id(issue_id: str) -> Tuple[int, str]:
    """``"1670Z22"`` -> ``(1670, "Z22")``; ``"0001"`` -> ``(1, "")``."""
    _, number, suffix = parse_id(issue_id)
    return number, suffix


def is_supplement(issue_id: str) -> bool:
    """Whether the id carries a trailing supplement marker.

    The leading prefix of a series like ``f0001`` is not a marker -- that is a
    parallel run of issues, not a supplement to any one of them.
    """
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


def sort_key(issue_id: str) -> Tuple[str, int, str]:
    try:
        prefix, number, suffix = parse_id(issue_id)
    except ValueError:
        return "\uffff", 10 ** 12, issue_id
    return prefix.lower(), number, suffix


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
