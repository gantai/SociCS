"""Fetching and validating the PDFs themselves."""

from __future__ import annotations

import os
from typing import Callable, Iterable, List, Optional, Tuple

from .client import BudgetExhausted, PoliteClient, Response, TransportError
from .collections import Collection
from .manifest import ERROR, MISSING, OK, Entry, Manifest

PDF_MAGIC = b"%PDF-"


def looks_like_pdf(head: bytes) -> bool:
    # Some servers emit a UTF-8 BOM or a stray newline before the header.
    return head.lstrip(b"\xef\xbb\xbf\r\n \t").startswith(PDF_MAGIC)


def inspect_file(path: str) -> Tuple[bool, bool, int]:
    """Return ``(is_pdf, has_eof_marker, size)`` for a file on disk."""
    size = os.path.getsize(path)
    with open(path, "rb") as handle:
        head = handle.read(len(PDF_MAGIC) + 8)
        tail_window = min(size, 4096)
        handle.seek(size - tail_window)
        tail = handle.read(tail_window)
    return looks_like_pdf(head), b"%%EOF" in tail, size


def probe(client: PoliteClient, collection: Collection, issue_id: str) -> Optional[bool]:
    """Cheap existence check. True/False, or None if we could not tell.

    Tries HEAD first because it costs no body. Servers that reject HEAD get a
    tiny ranged GET instead -- we only need the first few bytes to know whether
    this is a real PDF or a "not found" page dressed up as HTTP 200.
    """
    path = collection.pdf_path(issue_id)
    try:
        response = client.request("HEAD", path)
    except BudgetExhausted:
        raise
    except TransportError:
        return None

    if response.status == 404:
        return False
    if response.ok:
        ctype = response.header("content-type").lower()
        if "pdf" in ctype:
            return True
        if "html" in ctype or "text" in ctype:
            return False
        # Unhelpful content type -- fall through to the byte check.
    elif response.status not in (403, 405, 501):
        return None

    try:
        ranged = client.request(
            "GET", path, headers={"Range": "bytes=0-1023"}, max_bytes=1024
        )
    except BudgetExhausted:
        raise
    except TransportError:
        return None
    if ranged.status == 404:
        return False
    if ranged.ok or ranged.status == 206:
        return looks_like_pdf(ranged.body)
    return None


def download_one(
    client: PoliteClient,
    collection: Collection,
    issue_id: str,
    dest_dir: str,
    *,
    log: Callable[[str], None] = lambda msg: None,
) -> Entry:
    """Fetch one issue into ``dest_dir``.

    Writes to a ``.part`` file and only renames it into place once the bytes
    have been confirmed to be a PDF, so an interrupted or error-page response
    can never masquerade as a completed download.
    """
    filename = f"{issue_id}.pdf"
    final_path = os.path.join(dest_dir, filename)
    part_path = final_path + ".part"
    os.makedirs(dest_dir, exist_ok=True)

    try:
        with open(part_path, "wb") as sink:
            response = client.request(
                "GET", collection.pdf_path(issue_id), sink=sink
            )
    except BudgetExhausted:
        # The run is over, not this file. Leave no partial behind and let it
        # travel up so the caller stops rather than marking a false error.
        _unlink(part_path)
        raise
    except TransportError as exc:
        _unlink(part_path)
        return Entry(id=issue_id, status=ERROR, note=str(exc))

    if response.status == 404:
        _unlink(part_path)
        return Entry(id=issue_id, status=MISSING, http_status=404, note="not found")

    if not response.ok:
        _unlink(part_path)
        return Entry(
            id=issue_id,
            status=ERROR,
            http_status=response.status,
            note=f"HTTP {response.status} {response.reason}".strip(),
        )

    if response.size == 0:
        _unlink(part_path)
        return Entry(id=issue_id, status=MISSING, http_status=response.status, note="empty body")

    is_pdf, has_eof, size = inspect_file(part_path)
    if not is_pdf:
        # A 200 that is not a PDF is this server's way of saying "no such
        # issue"; keeping the bytes would poison the archive.
        _unlink(part_path)
        return Entry(
            id=issue_id,
            status=MISSING,
            http_status=response.status,
            note="response was not a PDF",
        )

    expected = response.header("content-length")
    if expected.isdigit() and int(expected) != size:
        _unlink(part_path)
        return Entry(
            id=issue_id,
            status=ERROR,
            http_status=response.status,
            note=f"truncated: got {size} of {expected} bytes",
        )

    os.replace(part_path, final_path)
    note = "" if has_eof else "no %%EOF marker (file may be truncated upstream)"
    if note:
        log(f"  ~ {issue_id}: {note}")
    return Entry(
        id=issue_id,
        status=OK,
        http_status=response.status,
        size=size,
        sha256=response.sha256,
        filename=filename,
        note=note,
    )


def download_all(
    client: PoliteClient,
    collection: Collection,
    issue_ids: Iterable[str],
    dest_dir: str,
    manifest: Manifest,
    *,
    recheck_missing: bool = False,
    retry_errors: bool = True,
    force: bool = False,
    log: Callable[[str], None] = print,
    save_every: int = 10,
) -> dict:
    """Download a list of issues, skipping anything already settled."""
    issue_ids = list(issue_ids)
    stats = {"downloaded": 0, "skipped": 0, "missing": 0, "errors": 0, "bytes": 0}
    if force:
        pending = list(issue_ids)
    else:
        pending = [
            i for i in issue_ids
            if not manifest.is_done(i, recheck_missing=recheck_missing)
            and (retry_errors or (manifest.get(i) or Entry(i, "")).status != ERROR)
        ]
    stats["skipped"] = len(issue_ids) - len(pending)
    if stats["skipped"]:
        log(f"Skipping {stats['skipped']} issue(s) already settled in the manifest.")

    try:
        for index, issue_id in enumerate(pending, 1):
            entry = download_one(client, collection, issue_id, dest_dir, log=log)
            manifest.record(entry)
            if entry.status == OK:
                stats["downloaded"] += 1
                stats["bytes"] += entry.size
                log(f"[{index}/{len(pending)}] {issue_id}  {_human(entry.size)}")
            elif entry.status == MISSING:
                stats["missing"] += 1
                log(f"[{index}/{len(pending)}] {issue_id}  -- absent ({entry.note})")
            else:
                stats["errors"] += 1
                log(f"[{index}/{len(pending)}] {issue_id}  !! {entry.note}")
            if index % save_every == 0:
                manifest.save()
    finally:
        manifest.save(force=True)
    return stats


def verify_dir(dest_dir: str, manifest: Manifest) -> List[Tuple[str, str]]:
    """Re-check downloaded files on disk. Returns ``(id, problem)`` pairs."""
    problems: List[Tuple[str, str]] = []
    for issue_id, entry in sorted(manifest.entries.items()):
        if entry.status != OK:
            continue
        path = os.path.join(dest_dir, entry.filename or f"{issue_id}.pdf")
        if not os.path.exists(path):
            problems.append((issue_id, "recorded as downloaded but file is gone"))
            continue
        is_pdf, has_eof, size = inspect_file(path)
        if not is_pdf:
            problems.append((issue_id, "file on disk is not a PDF"))
        elif entry.size and entry.size != size:
            problems.append((issue_id, f"size changed: manifest {entry.size}, disk {size}"))
        elif not has_eof:
            problems.append((issue_id, "missing %%EOF marker"))
    return problems


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"
