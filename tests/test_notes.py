"""Tests for the notes store + adapter + the matter override path in mapper."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from billable.adapters.notes import NotesAdapter
from billable.core.events import Event
from billable.core.mapper import ProjectMapper, ProjectRule
from billable.notes.store import Note, append_note, read_notes


def test_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "notes.jsonl"
    n1 = Note(
        timestamp=datetime(2026, 5, 8, 9, 14, 0, tzinfo=timezone.utc),
        text="Reviewed the Smith brief, returned with comments.",
        minutes=30,
        matter_id="smith-litigation",
    )
    n2 = Note(
        timestamp=datetime(2026, 5, 8, 11, 0, 0, tzinfo=timezone.utc),
        text="No-frills note",
    )
    append_note(n1, path=path)
    append_note(n2, path=path)
    notes = read_notes(path=path)
    assert notes == [n1, n2]


def test_read_skips_malformed_lines(tmp_path: Path) -> None:
    path = tmp_path / "notes.jsonl"
    path.write_text(
        '{"timestamp": "2026-05-08T09:00:00+00:00", "text": "good"}\n'
        "this is not json at all\n"
        '{"text": "missing timestamp"}\n'
        '{"timestamp": "2026-05-08T10:00:00+00:00", "text": "another good"}\n',
        encoding="utf-8",
    )
    notes = read_notes(path=path)
    assert len(notes) == 2
    assert notes[0].text == "good"
    assert notes[1].text == "another good"


def test_adapter_filters_by_local_date(tmp_path: Path) -> None:
    path = tmp_path / "notes.jsonl"
    local_tz = datetime.now().astimezone().tzinfo
    target = date(2026, 5, 8)

    in_range = datetime(2026, 5, 8, 14, 0, 0, tzinfo=local_tz)
    yesterday = in_range - timedelta(days=1)
    tomorrow = in_range + timedelta(days=1)

    for ts, text in [
        (yesterday, "yesterday"),
        (in_range, "today A"),
        (in_range + timedelta(hours=2), "today B"),
        (tomorrow, "tomorrow"),
    ]:
        append_note(Note(timestamp=ts, text=text), path=path)

    events = NotesAdapter(notes_path=path).capture(target)
    assert [e.content_excerpt.split("] ", 1)[-1] for e in events] == ["today A", "today B"]
    # Sorted by timestamp.
    assert events == sorted(events, key=lambda e: e.timestamp)


def test_adapter_emits_duration_when_minutes_given(tmp_path: Path) -> None:
    path = tmp_path / "notes.jsonl"
    local_tz = datetime.now().astimezone().tzinfo
    when = datetime(2026, 5, 8, 10, 0, 0, tzinfo=local_tz)
    append_note(
        Note(timestamp=when, text="met with team", minutes=45, matter_id="acme"),
        path=path,
    )
    events = NotesAdapter(notes_path=path).capture(date(2026, 5, 8))
    assert len(events) == 1
    e = events[0]
    assert e.duration == timedelta(minutes=45)
    assert e.raw["matter_id"] == "acme"
    # The matter prefix is exposed in the excerpt so keyword matchers see it too.
    assert e.content_excerpt.startswith("[matter:acme]")


def test_mapper_honors_raw_matter_id_override() -> None:
    """A note with --matter should bypass keyword/folder rules entirely."""
    mapper = ProjectMapper(
        rules=(
            ProjectRule(matter_id="acme", display_name="Acme", keywords=("totally-unrelated",)),
        )
    )
    e = Event(
        timestamp=datetime(2026, 5, 8, 10, 0, tzinfo=timezone.utc),
        duration=None,
        source="notes",
        project_hint="smith-litigation",
        artifact_ref="note:1",
        content_excerpt="reviewed brief",
        raw={"matter_id": "smith-litigation"},
    )
    # Even though no rule matches, the raw override wins.
    assert mapper.resolve(e) == "smith-litigation"


def test_mapper_ignores_empty_or_non_string_matter_override() -> None:
    mapper = ProjectMapper(rules=())
    e_empty = Event(
        timestamp=datetime(2026, 5, 8, 10, 0, tzinfo=timezone.utc),
        duration=None,
        source="notes",
        project_hint=None,
        artifact_ref="r1",
        content_excerpt="x",
        raw={"matter_id": ""},
    )
    e_int = Event(
        timestamp=datetime(2026, 5, 8, 10, 0, tzinfo=timezone.utc),
        duration=None,
        source="notes",
        project_hint=None,
        artifact_ref="r2",
        content_excerpt="x",
        raw={"matter_id": 123},
    )
    assert mapper.resolve(e_empty) is None
    assert mapper.resolve(e_int) is None


def test_missing_notes_file_returns_empty(tmp_path: Path) -> None:
    events = NotesAdapter(notes_path=tmp_path / "nope.jsonl").capture(date(2026, 5, 8))
    assert events == []
