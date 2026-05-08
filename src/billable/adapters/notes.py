"""Hotkey-triggered manual notes adapter.

Reads ``data/notes.jsonl`` (written by the ``billable note`` CLI command or
a hotkey-bound script) and emits one Event per note whose timestamp falls
on the target date.

These are by far the highest-signal events the system can produce, because
they capture *intent* and *conclusions* — what the user decided, not just
what the screen showed. Treat them as ground truth.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

from billable.adapters.base import CaptureAdapter
from billable.core.events import Event
from billable.core.sanitize import sanitize
from billable.notes.store import DEFAULT_NOTES_PATH, read_notes


class NotesAdapter(CaptureAdapter):
    name = "notes"

    def __init__(self, notes_path: Path | None = None) -> None:
        self.notes_path = notes_path or DEFAULT_NOTES_PATH

    def capture(self, target_date: date) -> list[Event]:
        events: list[Event] = []
        for i, note in enumerate(read_notes(self.notes_path)):
            if note.timestamp.date() != target_date:
                continue
            duration = (
                timedelta(minutes=note.minutes) if note.minutes is not None else None
            )
            # Notes can self-pin to a matter via the `matter_id` field. We
            # surface that through `project_hint` so the mapper picks it up
            # via the keywords matcher (which always runs).
            project_hint = note.matter_id
            artifact_ref = (
                f"note:{note.timestamp.isoformat()}:{i}"
            )
            # Make sure the matter_id, if present, also appears in the excerpt
            # so any keywords matcher targeting it fires too.
            excerpt_prefix = (
                f"[matter:{note.matter_id}] " if note.matter_id else ""
            )
            events.append(
                Event(
                    timestamp=note.timestamp,
                    duration=duration,
                    source="notes",
                    project_hint=project_hint,
                    artifact_ref=artifact_ref,
                    content_excerpt=sanitize(excerpt_prefix + note.text),
                    raw={"matter_id": note.matter_id, "minutes": note.minutes},
                )
            )
        events.sort(key=lambda e: e.timestamp)
        return events


def _today_aware() -> datetime:
    """Used by tests."""
    return datetime.now().astimezone()
