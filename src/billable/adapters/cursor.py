"""Cursor chat transcript adapter.

Reads chat / composer history from Cursor's local SQLite storage:

    Windows:  %APPDATA%\\Cursor\\User\\workspaceStorage\\<hash>\\state.vscdb
    macOS:    ~/Library/Application Support/Cursor/User/workspaceStorage/<hash>/state.vscdb
    Linux:    ~/.config/Cursor/User/workspaceStorage/<hash>/state.vscdb

Each workspace folder maps cleanly to a `project_hint` (the last segment of
the workspace's folder path on disk). The mapper turns that into a
`matter_id` via `config/projects.yaml`.

What we read (v1):

    Per workspace:
      ItemTable["aiService.generations"]  - JSON list of:
        { unixMs, generationUUID, type ("composer"|"chat"|"inline"|...),
          textDescription }
      workspace.json                       - { folder: "file:///..." } (or
                                              { workspace: "..." } for
                                              multi-root workspaces)

We emit one Event per generation whose `unixMs` falls on `target_date`
(local time). `content_excerpt` is `textDescription` (the user prompt that
triggered the generation), sanitized.

What we do NOT read (yet, see DESIGN.md §7):
    - Bubble-level transcripts from globalStorage's `cursorDiskKV`
      (richer signal but heavier; v2 will pull these for higher-quality
      narratives)
    - aiService.prompts (no timestamps; redundant given generations)
"""

from __future__ import annotations

import json
import logging
import os
import platform
import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

from billable.adapters.base import CaptureAdapter
from billable.core.events import Event
from billable.core.sanitize import sanitize

log = logging.getLogger(__name__)

# Maximum content_excerpt length we send downstream. Keeps token cost bounded
# without losing the substance of typical prompts. Long bodies get tail-truncated.
_MAX_EXCERPT_CHARS = 2000


class CursorAdapter(CaptureAdapter):
    name = "cursor"

    def __init__(self, storage_root: Path | None = None) -> None:
        """If `storage_root` is None, use the platform default."""
        self.storage_root = storage_root or self._default_storage_root()

    @staticmethod
    def _default_storage_root() -> Path:
        system = platform.system()
        home = Path.home()
        if system == "Windows":
            appdata = os.environ.get("APPDATA")
            base = Path(appdata) if appdata else home / "AppData" / "Roaming"
            return base / "Cursor" / "User" / "workspaceStorage"
        if system == "Darwin":
            return home / "Library" / "Application Support" / "Cursor" / "User" / "workspaceStorage"
        # Linux / other Unix.
        return home / ".config" / "Cursor" / "User" / "workspaceStorage"

    def capture(self, target_date: date) -> list[Event]:
        if not self.storage_root.exists():
            log.warning("Cursor storage root does not exist: %s", self.storage_root)
            return []

        # Local-day boundaries — convert to a unix-ms range so we can compare
        # against `unixMs` directly.
        local_tz = datetime.now().astimezone().tzinfo
        day_start = datetime.combine(target_date, datetime.min.time(), tzinfo=local_tz)
        day_end = day_start + timedelta(days=1)
        start_ms = int(day_start.astimezone(timezone.utc).timestamp() * 1000)
        end_ms = int(day_end.astimezone(timezone.utc).timestamp() * 1000)

        events: list[Event] = []
        for workspace_dir in sorted(self.storage_root.iterdir()):
            if not workspace_dir.is_dir():
                continue
            try:
                events.extend(
                    self._capture_one_workspace(workspace_dir, start_ms, end_ms, local_tz)
                )
            except Exception as e:
                # Per-workspace failures shouldn't take the whole capture down.
                log.warning("Skipping Cursor workspace %s: %s", workspace_dir.name, e)

        events.sort(key=lambda e: e.timestamp)
        return events

    # -- internals -----------------------------------------------------------

    def _capture_one_workspace(
        self,
        workspace_dir: Path,
        start_ms: int,
        end_ms: int,
        local_tz: object,
    ) -> list[Event]:
        db_path = workspace_dir / "state.vscdb"
        if not db_path.exists():
            return []

        project_hint = self._workspace_folder_name(workspace_dir / "workspace.json")
        workspace_id = workspace_dir.name

        # NB: do NOT use immutable=1 — Cursor uses WAL and immutable hides
        # uncommitted writes that contain today's data.
        uri = f"file:{db_path.as_posix()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=2.0)) as con:
            row = con.execute(
                "SELECT value FROM ItemTable WHERE key = 'aiService.generations'"
            ).fetchone()
            if not row or not row[0]:
                return []
            try:
                generations = json.loads(row[0])
            except json.JSONDecodeError:
                log.warning("Could not parse aiService.generations in %s", workspace_id)
                return []

        events: list[Event] = []
        for gen in generations:
            if not isinstance(gen, dict):
                continue
            unix_ms = gen.get("unixMs")
            if not isinstance(unix_ms, int) or not (start_ms <= unix_ms < end_ms):
                continue

            text = (gen.get("textDescription") or "").strip()
            if not text:
                continue

            ts = datetime.fromtimestamp(unix_ms / 1000, tz=timezone.utc).astimezone(local_tz)
            generation_uuid = gen.get("generationUUID") or "unknown"

            excerpt = sanitize(_truncate(text, _MAX_EXCERPT_CHARS))

            events.append(
                Event(
                    timestamp=ts,
                    duration=None,  # generations are point-in-time events
                    source="cursor",
                    project_hint=project_hint,
                    artifact_ref=f"cursor:{workspace_id}:{generation_uuid}",
                    content_excerpt=excerpt,
                    raw={
                        "workspace_id": workspace_id,
                        "type": gen.get("type"),
                        "generationUUID": generation_uuid,
                    },
                )
            )
        return events

    @staticmethod
    def _workspace_folder_name(workspace_json: Path) -> str | None:
        """Derive a friendly project hint from workspace.json.

        Returns the last segment of the workspace folder path, or None if the
        workspace is remote / multi-root / untitled / can't be parsed.
        """
        if not workspace_json.exists():
            return None
        try:
            data = json.loads(workspace_json.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

        folder = data.get("folder")
        if not folder:
            # Multi-root workspaces use {"workspace": "..."} pointing at a .code-workspace file.
            ws = data.get("workspace")
            if isinstance(ws, str) and ws:
                # Best-effort: filename without extension.
                name = Path(unquote(urlparse(ws).path)).stem
                return name or None
            return None

        try:
            parsed = urlparse(folder)
        except ValueError:
            return None

        # file:///c:/... or file:///home/jason/...
        path_str = unquote(parsed.path)
        # On Windows the leading slash precedes the drive letter; strip it.
        if path_str.startswith("/") and len(path_str) > 2 and path_str[2] == ":":
            path_str = path_str[1:]
        name = Path(path_str).name
        return name or None


def _truncate(s: str, limit: int) -> str:
    if len(s) <= limit:
        return s
    return s[:limit] + f"… [truncated {len(s) - limit} chars]"
