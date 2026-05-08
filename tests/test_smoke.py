"""Skeleton smoke tests — verify imports work and dataclasses construct.

These tests deliberately do not exercise any stubbed (NotImplementedError)
code paths. They are here so `pytest -q` passes on the skeleton, giving us
a green baseline before any implementation lands.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal


def test_version_importable() -> None:
    from billable import __version__

    assert isinstance(__version__, str) and __version__


def test_event_constructs() -> None:
    from billable.core.events import Event

    e = Event(
        timestamp=datetime(2026, 5, 7, 9, 0, 0),
        duration=timedelta(minutes=30),
        source="cursor",
        project_hint="billable",
        artifact_ref="cursor:abc:conv1:turn1",
        content_excerpt="planned the activity-tracking agent",
    )
    assert e.source == "cursor"
    assert e.duration == timedelta(minutes=30)


def test_session_duration() -> None:
    from billable.core.events import Session

    s = Session(
        start=datetime(2026, 5, 7, 9, 0, 0),
        end=datetime(2026, 5, 7, 10, 30, 0),
        matter_id="internal-billable-agent",
        events=[],
        category="planning",
    )
    assert s.duration == timedelta(minutes=90)


def test_entry_constructs() -> None:
    from billable.core.events import Entry

    entry = Entry(
        date=date(2026, 5, 7),
        matter_id="internal-billable-agent",
        description="Drafted architecture plan for activity-tracking agent.",
        hours=Decimal("1.25"),
        sources=["cursor:abc:conv1:turn1"],
    )
    assert entry.task_code is None  # nullable today, used by legal mode later
    assert entry.activity_code is None


def test_cli_help_runs() -> None:
    """The CLI must at least parse and print help on the skeleton."""
    from typer.testing import CliRunner

    from billable.cli import app

    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "billable" in result.output.lower()
