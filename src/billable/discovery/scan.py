"""Observe what's on this machine, with no opinions and no LLM.

The output of ``scan_machine`` is a structured snapshot that the LLM-side
``propose_projects_yaml`` consumes to draft a ``projects.yaml``. Keeping
scanning separate from the LLM call lets the CLI show the user the raw
findings (and lets us unit-test the scanning logic without hitting any
network).

What we observe today (cheap, local-only):

    - Cursor workspaces with at least one prompt in the last `days`.
      For each: folder name, total prompt count, a few sample prompts,
      and most-recent activity timestamp.
    - ActivityWatch window-title patterns grouped by (app, normalized
      title prefix), ranked by total focus minutes. Cursor windows are
      excluded — they're already covered by the Cursor workspace list.
    - Notes: count only (their bodies are private and short).

Things we deliberately don't observe (yet): browser history, system
file activity, calendar entries. v2 territory.
"""

from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from billable.adapters.activitywatch import (
    ActivityWatchAdapter,
    extract_cursor_workspace,
)
from billable.adapters.cursor import CursorAdapter

log = logging.getLogger(__name__)

# How many sample prompts to show per workspace. Enough for the LLM to
# infer what the project is about; small enough to keep the prompt cheap.
_MAX_SAMPLE_PROMPTS = 5

# Trim long prompt samples so one rambling prompt doesn't blow the
# discover-prompt token budget.
_MAX_SAMPLE_LEN = 240

# Top-N browser patterns to surface to the LLM. Beyond ~30 the model
# starts treating noise as signal.
_MAX_BROWSER_PATTERNS = 30

# How many leading words of a window title to keep when grouping.
# "AIBuddy and 15 more pages - Profile 1 - Microsoft Edge" and
# "AIBuddy and 7 more pages - Profile 1 - Microsoft Edge" both bucket
# under "AIBuddy".
_TITLE_PREFIX_WORDS = 3


@dataclass(frozen=True)
class CursorWorkspace:
    """One Cursor workspace observed on this machine."""

    folder_name: str
    prompt_count: int
    last_seen: datetime
    sample_prompts: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "folder_name": self.folder_name,
            "prompt_count": self.prompt_count,
            "last_seen": self.last_seen.isoformat(),
            "sample_prompts": list(self.sample_prompts),
        }


@dataclass(frozen=True)
class BrowserPattern:
    """A recurring non-Cursor window-title pattern from ActivityWatch."""

    app: str
    title_prefix: str
    occurrence_count: int
    total_minutes: int
    example_titles: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "app": self.app,
            "title_prefix": self.title_prefix,
            "occurrence_count": self.occurrence_count,
            "total_minutes": self.total_minutes,
            "example_titles": list(self.example_titles),
        }


@dataclass(frozen=True)
class DiscoveryScan:
    """Snapshot of what we found on this machine."""

    days_scanned: int
    cursor_workspaces: tuple[CursorWorkspace, ...] = ()
    browser_patterns: tuple[BrowserPattern, ...] = ()
    note_count: int = 0
    activitywatch_available: bool = False

    def to_dict(self) -> dict:
        return {
            "days_scanned": self.days_scanned,
            "cursor_workspaces": [w.to_dict() for w in self.cursor_workspaces],
            "browser_patterns": [p.to_dict() for p in self.browser_patterns],
            "note_count": self.note_count,
            "activitywatch_available": self.activitywatch_available,
        }


def scan_machine(
    *,
    days: int = 7,
    cursor_adapter: CursorAdapter | None = None,
    aw_adapter: ActivityWatchAdapter | None = None,
    today: date | None = None,
) -> DiscoveryScan:
    """Walk Cursor + ActivityWatch + notes for the last `days` days.

    All adapters are optional and instantiated with their defaults when
    omitted. Adapters that fail (e.g. ActivityWatch not running) degrade
    silently — we'd rather surface a partial scan than crash.
    """
    cursor_adapter = cursor_adapter or CursorAdapter()
    aw_adapter = aw_adapter or ActivityWatchAdapter()
    today = today or date.today()

    workspaces = _scan_cursor(cursor_adapter, days, today)
    aw_available, browser = _scan_browser(aw_adapter, days, today)
    notes = _scan_notes(days, today)

    return DiscoveryScan(
        days_scanned=days,
        cursor_workspaces=workspaces,
        browser_patterns=browser,
        note_count=notes,
        activitywatch_available=aw_available,
    )


# --- Cursor scan ------------------------------------------------------------


def _scan_cursor(
    adapter: CursorAdapter, days: int, today: date
) -> tuple[CursorWorkspace, ...]:
    by_workspace: dict[str, _WorkspaceAccumulator] = defaultdict(_WorkspaceAccumulator)
    for offset in range(days):
        d = today - timedelta(days=offset)
        try:
            events = adapter.capture(d)
        except Exception as e:
            log.warning("Cursor scan failed for %s: %s", d, e)
            continue
        for e in events:
            if e.source != "cursor" or not e.project_hint:
                continue
            acc = by_workspace[e.project_hint]
            acc.count += 1
            acc.samples.append(e.content_excerpt or "")
            if acc.last_seen is None or e.timestamp > acc.last_seen:
                acc.last_seen = e.timestamp

    out: list[CursorWorkspace] = []
    for folder_name, acc in by_workspace.items():
        samples = _pick_sample_prompts(acc.samples)
        out.append(
            CursorWorkspace(
                folder_name=folder_name,
                prompt_count=acc.count,
                last_seen=acc.last_seen or datetime.now().astimezone(),
                sample_prompts=samples,
            )
        )
    out.sort(key=lambda w: w.prompt_count, reverse=True)
    return tuple(out)


@dataclass
class _WorkspaceAccumulator:
    count: int = 0
    last_seen: datetime | None = None
    samples: list[str] = field(default_factory=list)


def _pick_sample_prompts(prompts: list[str]) -> tuple[str, ...]:
    """Pick a handful of distinctive prompts to characterize a project.

    Heuristic: drop near-duplicates (same first 60 chars), prefer the
    longer prompts (likely more substantive), then trim to MAX_SAMPLE_LEN.
    """
    seen_prefixes: set[str] = set()
    unique: list[str] = []
    for p in sorted(prompts, key=len, reverse=True):
        prefix = p[:60].strip().lower()
        if not prefix or prefix in seen_prefixes:
            continue
        seen_prefixes.add(prefix)
        unique.append(p[:_MAX_SAMPLE_LEN].strip())
        if len(unique) >= _MAX_SAMPLE_PROMPTS:
            break
    return tuple(unique)


# --- ActivityWatch (browser-only) scan -------------------------------------


def _scan_browser(
    adapter: ActivityWatchAdapter, days: int, today: date
) -> tuple[bool, tuple[BrowserPattern, ...]]:
    """Group recurring non-Cursor window patterns by (app, title prefix)."""
    occurrences: Counter[tuple[str, str]] = Counter()
    minutes: Counter[tuple[str, str]] = Counter()
    examples: dict[tuple[str, str], list[str]] = defaultdict(list)
    server_ok = False

    for offset in range(days):
        d = today - timedelta(days=offset)
        try:
            events = adapter.capture(d)
            server_ok = server_ok or bool(events) or _ping_aw(adapter)
        except Exception as e:
            log.warning("AW scan failed for %s: %s", d, e)
            continue
        for e in events:
            raw = e.raw or {}
            app = str(raw.get("app") or "")
            title = str(raw.get("title") or "")
            if not app or not title:
                continue
            # Skip Cursor windows — they're covered by the Cursor workspace
            # scan and would double-count.
            if extract_cursor_workspace(app, title):
                continue
            key = (app, _title_prefix(title))
            occurrences[key] += 1
            minutes[key] += int((e.duration or timedelta()).total_seconds() / 60)
            if title not in examples[key] and len(examples[key]) < 3:
                examples[key].append(title)

    patterns: list[BrowserPattern] = []
    for (app, prefix), count in occurrences.most_common(_MAX_BROWSER_PATTERNS):
        patterns.append(
            BrowserPattern(
                app=app,
                title_prefix=prefix,
                occurrence_count=count,
                total_minutes=minutes[(app, prefix)],
                example_titles=tuple(examples[(app, prefix)]),
            )
        )
    return server_ok, tuple(patterns)


def _ping_aw(adapter: ActivityWatchAdapter) -> bool:
    try:
        adapter._list_buckets()  # noqa: SLF001 — using adapter's HTTP helper
        return True
    except Exception:
        return False


_TITLE_NOISE = re.compile(
    r"\s+and\s+\d+\s+more\s+pages?\s*",  # "AIBuddy and 15 more pages"
    re.IGNORECASE,
)


def _title_prefix(title: str) -> str:
    """Reduce a window title to its grouping key.

    Strips the variable "and N more pages" suffix, then keeps the first
    `_TITLE_PREFIX_WORDS` words. Ties together titles like
    ``"AIBuddy and 15 more pages"`` and ``"AIBuddy and 8 more pages"``.
    """
    cleaned = _TITLE_NOISE.sub(" ", title)
    words = cleaned.split()
    if not words:
        return title.strip()[:60]
    return " ".join(words[:_TITLE_PREFIX_WORDS])


# --- Notes scan -------------------------------------------------------------


def _scan_notes(days: int, today: date) -> int:
    from billable.notes.store import DEFAULT_NOTES_PATH, read_notes

    if not Path(DEFAULT_NOTES_PATH).exists():
        return 0
    try:
        notes = read_notes(Path(DEFAULT_NOTES_PATH))
    except Exception as e:
        log.warning("Notes scan failed: %s", e)
        return 0
    cutoff = today - timedelta(days=days)
    return sum(1 for n in notes if n.timestamp.date() >= cutoff)
