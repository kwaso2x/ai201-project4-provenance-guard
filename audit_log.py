"""Structured, append-only audit log for Provenance Guard.

Every attribution decision (and later, every appeal) gets written here as a
JSON object, one per line (JSONL). I keep an in-memory copy for fast reads and
also persist to a file so entries survive a restart. This is the source of the
GET /log output and the audit trail the project requires.
"""

import json
import os
import threading
from datetime import datetime, timezone

LOG_PATH = os.path.join(os.path.dirname(__file__), "audit_log.jsonl")

_entries = []
_lock = threading.Lock()  # /submit can be hit concurrently; keep writes safe


def _load():
    """Load any existing entries from disk on startup."""
    if not os.path.exists(LOG_PATH):
        return
    with open(LOG_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    _entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass  # skip a corrupt line rather than crash


_load()


def now_iso():
    """Current UTC time as an ISO-8601 string with a trailing Z."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def append(entry):
    """Append a structured entry (a dict) to the log, in memory and on disk."""
    with _lock:
        _entries.append(entry)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    return entry


def get_log(limit=None):
    """Return entries, most recent first. Optionally cap the count."""
    with _lock:
        items = list(reversed(_entries))
    return items[:limit] if limit else items
