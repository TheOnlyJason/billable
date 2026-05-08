"""Core data model — Event, Session, Entry.

These three dataclasses are the contract that the entire pipeline is shaped
around. See DESIGN.md §4 for the reasoning behind each field.

Stability promise:
    - Adding new optional fields is fine.
    - Renaming or removing fields is a breaking change for every adapter,
      every prompt, every renderer, and any cached event JSON in `cache/`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any


@dataclass
class Event:
    """A single normalized activity event from a capture adapter.

    Adapters MUST emit events in this shape. The `raw` payload is kept so we
    can re-derive sessions and entries with a new prompt without re-capturing.
    """

    timestamp: datetime
    duration: timedelta | None
    source: str
    project_hint: str | None
    artifact_ref: str
    content_excerpt: str
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Session:
    """An LLM-inferred coherent block of work, before per-day rollup.

    A session has one matter and one general category. Multiple sessions on
    the same matter on the same day collapse into a single Entry.
    """

    start: datetime
    end: datetime
    matter_id: str | None
    events: list[Event]
    category: str
    summary_hint: str = ""

    @property
    def duration(self) -> timedelta:
        return self.end - self.start


@dataclass
class Entry:
    """One row on the final timesheet.

    `task_code` and `activity_code` are nullable today — they turn on when
    the legal renderer is added in v2. See DESIGN.md §4.
    """

    date: date
    matter_id: str
    description: str
    hours: Decimal
    sources: list[str] = field(default_factory=list)
    task_code: str | None = None
    activity_code: str | None = None
