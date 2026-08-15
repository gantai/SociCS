"""Run state.

The manifest is what makes the job resumable, which is the main tool we have
for keeping load off the server: an interrupted run must never re-fetch what it
already has. It is written atomically so a crash mid-save cannot corrupt it.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from typing import Dict, Iterable, Optional

VERSION = 1

# Terminal states.
OK = "ok"            # a valid PDF is on disk
MISSING = "missing"  # server says the issue does not exist (404 / not a PDF)
# Retryable states.
ERROR = "error"      # transport or server-side failure


@dataclass
class Entry:
    id: str
    status: str
    http_status: Optional[int] = None
    size: int = 0
    sha256: str = ""
    filename: str = ""
    note: str = ""
    attempts: int = 0
    updated_at: float = field(default_factory=time.time)


class Manifest:
    def __init__(self, path: str, entries: Optional[Dict[str, Entry]] = None):
        self.path = path
        self.entries: Dict[str, Entry] = entries or {}
        self._dirty = False

    # -- persistence -----------------------------------------------------------

    @classmethod
    def load(cls, path: str) -> "Manifest":
        if not os.path.exists(path):
            return cls(path)
        with open(path, "r", encoding="utf-8") as handle:
            try:
                raw = json.load(handle)
            except json.JSONDecodeError:
                # A truncated manifest should not cost the user their progress
                # record; keep it aside instead of overwriting it.
                os.replace(path, path + ".corrupt")
                return cls(path)
        entries = {}
        for key, value in (raw.get("entries") or {}).items():
            known = {k: v for k, v in value.items() if k in Entry.__dataclass_fields__}
            known.setdefault("id", key)
            known.setdefault("status", ERROR)
            entries[key] = Entry(**known)
        return cls(path, entries)

    def save(self, force: bool = False) -> None:
        if not self._dirty and not force:
            return
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        payload = {
            "version": VERSION,
            "saved_at": time.time(),
            "entries": {k: asdict(v) for k, v in sorted(self.entries.items())},
        }
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=directory, prefix=".manifest-", suffix=".tmp", delete=False
        )
        try:
            with handle:
                json.dump(payload, handle, ensure_ascii=False, indent=1, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(handle.name, self.path)
        except BaseException:
            try:
                os.unlink(handle.name)
            except OSError:
                pass
            raise
        self._dirty = False

    # -- access ----------------------------------------------------------------

    def get(self, issue_id: str) -> Optional[Entry]:
        return self.entries.get(issue_id)

    def record(self, entry: Entry) -> None:
        previous = self.entries.get(entry.id)
        if previous is not None:
            entry.attempts = previous.attempts + 1
        else:
            entry.attempts = 1
        entry.updated_at = time.time()
        self.entries[entry.id] = entry
        self._dirty = True

    def is_done(self, issue_id: str, recheck_missing: bool = False) -> bool:
        """Whether we can skip this id without asking the server again."""
        entry = self.entries.get(issue_id)
        if entry is None:
            return False
        if entry.status == OK:
            return True
        if entry.status == MISSING and not recheck_missing:
            return True
        return False

    def counts(self) -> Dict[str, int]:
        tally: Dict[str, int] = {}
        for entry in self.entries.values():
            tally[entry.status] = tally.get(entry.status, 0) + 1
        return tally

    def ids_with_status(self, status: str) -> Iterable[str]:
        return (k for k, v in sorted(self.entries.items()) if v.status == status)
