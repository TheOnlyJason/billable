"""Hotkey-triggered manual notes.

Notes are user-generated input (not derived data), so they live under
``data/`` rather than ``cache/``. The cache directory is safe to delete;
``data/notes.jsonl`` should be preserved.

File format: one JSON object per line.

    {
      "timestamp": "2026-05-08T09:14:00-07:00",
      "text": "Reviewed the Smith brief, returned with comments.",
      "minutes": 30,                              // optional
      "matter_id": "smith-litigation"             // optional, pins to a project
    }

The CLI `billable note "..."` appends to this file. The NotesAdapter
reads it back at capture time.
"""
