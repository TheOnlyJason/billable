"""Read/write helpers for the JSONL notes store.

The file is append-only and crash-safe: each line is a complete JSON object,
written with `\n` at the end. Partial writes corrupt only the trailing line,
which the reader simply skips with a warning.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_NOTES_PATH = Path(
    os.environ.get("BILLABLE_NOTES_PATH", "./data/notes.jsonl")
)


@dataclass(frozen=True)
class Note:
    timestamp: datetime
    text: str
    minutes: int | None = None
    matter_id: str | None = None


def append_note(note: Note, *, path: Path = DEFAULT_NOTES_PATH) -> None:
    """Append one note to the JSONL store. Creates parent dirs if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict = {
        "timestamp": note.timestamp.isoformat(),
        "text": note.text,
    }
    if note.minutes is not None:
        payload["minutes"] = note.minutes
    if note.matter_id is not None:
        payload["matter_id"] = note.matter_id
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False))
        f.write("\n")


def read_notes(path: Path = DEFAULT_NOTES_PATH) -> list[Note]:
    """Read all notes from the JSONL store. Skips malformed lines with a warning."""
    if not path.exists():
        return []
    notes: list[Note] = []
    with path.open("r", encoding="utf-8") as f:
        for lineno, raw in enumerate(f, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                d = json.loads(raw)
                notes.append(
                    Note(
                        timestamp=datetime.fromisoformat(d["timestamp"]),
                        text=str(d["text"]),
                        minutes=d.get("minutes"),
                        matter_id=d.get("matter_id"),
                    )
                )
            except (json.JSONDecodeError, KeyError, ValueError) as e:
                log.warning("Skipping malformed note at %s:%d: %s", path, lineno, e)
    return notes
