"""Capture adapters — one per source of activity.

Each adapter implements `CaptureAdapter` (see `base.py`) and emits normalized
`Event` objects. Adding a new source is a matter of writing one file here and
registering it in the CLI's adapter list.

v1 ships with:
    - cursor       : Cursor chat transcripts from local SQLite storage
    - google_docs  : Google Docs activity via the Drive Activity API

Planned for later (DESIGN.md §7):
    - git           : commits + diffs across repos
    - calendar      : Google Calendar / Outlook
    - notes         : hotkey-triggered manual notes
    - activitywatch : time allocation, not content
"""
