"""ActivityWatch capture adapter.

ActivityWatch (https://activitywatch.net) is an open-source local activity
tracker. It runs as a tray app, watches your active window and AFK status,
and exposes the data via a REST API at http://localhost:5600.

This adapter:
    1. Lists buckets, finds the window-watcher and AFK-watcher buckets for
       this host.
    2. Pulls all events for the target date (local-day boundaries).
    3. Subtracts AFK time from window time, so a 2-hour window event with
       30 minutes AFK in the middle becomes 1.5 hours of active focus.
    4. Groups consecutive same-(app, title) events into "focus blocks."
    5. Drops focus blocks shorter than `min_focus_block_minutes` (default 2)
       to filter out noise like 10-second tab flicks.
    6. Emits one Event per focus block.

Setup is one-time: download ActivityWatch, run it, leave it running. No
configuration needed; the adapter discovers buckets at capture time.

Required: nothing. The adapter degrades gracefully when ActivityWatch is
not running (returns empty list with a warning).
"""

from __future__ import annotations

import logging
import socket
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

from billable.adapters.base import CaptureAdapter
from billable.core.events import Event
from billable.core.sanitize import sanitize

# Cursor windows almost always end in " - Cursor" and follow the pattern
# "<doc-or-tab> - <workspace-folder-name> - Cursor". We extract the
# second-to-last " - "-separated segment as the workspace, which becomes
# `project_hint` and lets ProjectMapper route the focus block to the right
# matter (or auto-discover one) without the user writing yaml.
_CURSOR_APP_SUFFIXES = ("cursor.exe", "cursor")
_CURSOR_TITLE_SUFFIX = " - Cursor"

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://localhost:5600"
DEFAULT_MIN_FOCUS_MINUTES = 2

# How long a gap can be inside the same (app, title) before we split into
# two focus blocks. ActivityWatch heartbeats fire about every 5 seconds, so
# 60 seconds is a generous threshold that tolerates brief alt-tabs.
_MERGE_GAP_SECONDS = 60


@dataclass(frozen=True)
class FocusBlock:
    start: datetime
    end: datetime
    app: str
    title: str

    @property
    def duration(self) -> timedelta:
        return self.end - self.start


class ActivityWatchAdapter(CaptureAdapter):
    name = "activitywatch"

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        min_focus_minutes: float = DEFAULT_MIN_FOCUS_MINUTES,
        timeout_seconds: float = 5.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.min_focus = timedelta(minutes=min_focus_minutes)
        self.timeout = timeout_seconds

    def capture(self, target_date: date) -> list[Event]:
        try:
            buckets = self._list_buckets()
        except _AWUnavailable as e:
            log.warning("ActivityWatch not reachable at %s: %s", self.base_url, e)
            return []

        window_bucket = _pick_bucket(buckets, "aw-watcher-window")
        afk_bucket = _pick_bucket(buckets, "aw-watcher-afk")
        if not window_bucket:
            log.warning("No aw-watcher-window bucket found; ActivityWatch not configured?")
            return []

        local_tz = datetime.now().astimezone().tzinfo
        day_start = datetime.combine(target_date, datetime.min.time(), tzinfo=local_tz)
        day_end = day_start + timedelta(days=1)

        window_events = self._fetch_events(window_bucket, day_start, day_end)
        afk_events = self._fetch_events(afk_bucket, day_start, day_end) if afk_bucket else []

        afk_intervals = _afk_intervals(afk_events)
        active_window_events = _subtract_afk(window_events, afk_intervals)
        focus_blocks = _merge_into_blocks(active_window_events, _MERGE_GAP_SECONDS)
        focus_blocks = [b for b in focus_blocks if b.duration >= self.min_focus]

        events: list[Event] = []
        for block in focus_blocks:
            ts = block.start.astimezone(local_tz)
            duration_minutes = int(block.duration.total_seconds() / 60)
            excerpt = sanitize(
                f"Focused on '{block.title}' in {block.app} for {duration_minutes} min."
            )
            # If this is a Cursor window, lift the workspace folder name out
            # of the title and use it as project_hint. That gives the mapper
            # a stable identifier (matches `cursor_workspaces` rules and
            # triggers the zero-config fallback) instead of the noisy full
            # window title. For non-Cursor windows leave it None — keyword
            # matching still operates on `content_excerpt`.
            project_hint = extract_cursor_workspace(block.app, block.title)
            events.append(
                Event(
                    timestamp=ts,
                    duration=block.duration,
                    source="activitywatch",
                    project_hint=project_hint,
                    artifact_ref=f"aw:{ts.isoformat()}:{_slug(block.app)}",
                    content_excerpt=excerpt,
                    raw={"app": block.app, "title": block.title},
                )
            )
        return events

    # -- internals -----------------------------------------------------------

    def _list_buckets(self) -> dict[str, dict[str, Any]]:
        return _http_json(f"{self.base_url}/api/0/buckets/", self.timeout) or {}

    def _fetch_events(
        self, bucket_id: str, start: datetime, end: datetime
    ) -> list[dict[str, Any]]:
        start_iso = start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        end_iso = end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        url = (
            f"{self.base_url}/api/0/buckets/{quote(bucket_id, safe='')}/events"
            f"?start={start_iso}&end={end_iso}&limit=-1"
        )
        return _http_json(url, self.timeout) or []


# --- pure helpers (testable without a running ActivityWatch) ----------------


def extract_cursor_workspace(app: str, title: str) -> str | None:
    """If `(app, title)` is a Cursor window, return the workspace folder name.

    Cursor uses the title pattern ``"<doc-or-tab> - <workspace> - Cursor"``,
    e.g. ``".env - bot-1 - Cursor"`` or
    ``"Clinical Documentation Review - hp-revision-portal - Cursor"``.

    We extract the second-to-last ``" - "``-separated segment, which is the
    workspace folder name. Returns None for non-Cursor windows or for
    Cursor windows that don't follow the pattern (e.g. the welcome screen).
    """
    if not app or not title:
        return None
    if app.lower() not in _CURSOR_APP_SUFFIXES and not app.lower().endswith("cursor.exe"):
        return None
    if not title.endswith(_CURSOR_TITLE_SUFFIX):
        return None
    parts = [p.strip() for p in title.split(" - ")]
    # Need at least: <doc> - <workspace> - Cursor
    if len(parts) < 3:
        return None
    workspace = parts[-2]
    if not workspace:
        return None
    return workspace


def _pick_bucket(buckets: dict[str, dict[str, Any]], type_prefix: str) -> str | None:
    """ActivityWatch bucket IDs look like 'aw-watcher-window_<hostname>'.

    Prefer one whose suffix matches the local hostname; fall back to any
    bucket whose ID starts with the prefix.
    """
    hostname = socket.gethostname().lower()
    candidates = [b for b in buckets if b.startswith(type_prefix)]
    for b in candidates:
        if b.lower().endswith(f"_{hostname}"):
            return b
    return candidates[0] if candidates else None


def _afk_intervals(afk_events: list[dict[str, Any]]) -> list[tuple[datetime, datetime]]:
    """Return list of (start, end) intervals during which the user was AFK."""
    out: list[tuple[datetime, datetime]] = []
    for e in afk_events:
        if (e.get("data") or {}).get("status") != "afk":
            continue
        try:
            start = datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        dur = float(e.get("duration") or 0)
        out.append((start, start + timedelta(seconds=dur)))
    return out


def _subtract_afk(
    window_events: list[dict[str, Any]],
    afk_intervals: list[tuple[datetime, datetime]],
) -> list[dict[str, Any]]:
    """Trim each window event so it doesn't overlap any AFK interval.

    A single window event split by AFK becomes multiple smaller events.
    """
    result: list[dict[str, Any]] = []
    for w in window_events:
        try:
            start = datetime.fromisoformat(w["timestamp"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        end = start + timedelta(seconds=float(w.get("duration") or 0))
        # Subtract each AFK interval one at a time.
        active_pieces: list[tuple[datetime, datetime]] = [(start, end)]
        for afk_start, afk_end in afk_intervals:
            new_pieces: list[tuple[datetime, datetime]] = []
            for ps, pe in active_pieces:
                if afk_end <= ps or afk_start >= pe:
                    new_pieces.append((ps, pe))
                    continue
                if afk_start > ps:
                    new_pieces.append((ps, min(afk_start, pe)))
                if afk_end < pe:
                    new_pieces.append((max(afk_end, ps), pe))
            active_pieces = new_pieces
        for ps, pe in active_pieces:
            if pe <= ps:
                continue
            result.append(
                {
                    "timestamp": ps.isoformat(),
                    "duration": (pe - ps).total_seconds(),
                    "data": w.get("data") or {},
                }
            )
    return result


def _merge_into_blocks(
    window_events: list[dict[str, Any]], gap_seconds: int
) -> list[FocusBlock]:
    """Group consecutive same-(app, title) events into FocusBlocks.

    A gap larger than `gap_seconds` between events of the same (app, title)
    starts a new block.
    """
    blocks: list[FocusBlock] = []
    parsed: list[tuple[datetime, datetime, str, str]] = []
    for w in window_events:
        try:
            start = datetime.fromisoformat(w["timestamp"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        end = start + timedelta(seconds=float(w.get("duration") or 0))
        data = w.get("data") or {}
        app = str(data.get("app") or "(unknown)")
        title = str(data.get("title") or "(no title)")
        parsed.append((start, end, app, title))
    parsed.sort(key=lambda t: t[0])

    for start, end, app, title in parsed:
        if blocks:
            last = blocks[-1]
            if (
                last.app == app
                and last.title == title
                and (start - last.end).total_seconds() <= gap_seconds
            ):
                blocks[-1] = FocusBlock(
                    start=last.start,
                    end=max(last.end, end),
                    app=app,
                    title=title,
                )
                continue
        blocks.append(FocusBlock(start=start, end=end, app=app, title=title))
    return blocks


def _slug(text: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in text)[:40] or "x"


# --- HTTP helpers -----------------------------------------------------------


class _AWUnavailable(RuntimeError):
    pass


def _http_json(url: str, timeout: float) -> Any:
    """Minimal JSON HTTP GET. Avoids adding a `requests` dependency."""
    import json
    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise _AWUnavailable(str(e)) from e
