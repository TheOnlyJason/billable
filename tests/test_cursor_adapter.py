"""Tests for CursorAdapter using synthetic SQLite databases.

These tests do NOT depend on the user's actual Cursor install. They build a
state.vscdb in a tmpdir that mirrors the real schema and verify the adapter
extracts events correctly.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from billable.adapters.cursor import CursorAdapter


def _make_workspace(
    storage_root: Path,
    workspace_id: str,
    folder_uri: str | None,
    generations: list[dict] | None,
) -> Path:
    ws_dir = storage_root / workspace_id
    ws_dir.mkdir(parents=True)

    if folder_uri is not None:
        (ws_dir / "workspace.json").write_text(
            json.dumps({"folder": folder_uri}), encoding="utf-8"
        )

    db_path = ws_dir / "state.vscdb"
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value TEXT)")
    if generations is not None:
        con.execute(
            "INSERT INTO ItemTable (key, value) VALUES (?, ?)",
            ("aiService.generations", json.dumps(generations)),
        )
    con.commit()
    con.close()
    return ws_dir


def _ms(dt: datetime) -> int:
    return int(dt.astimezone(timezone.utc).timestamp() * 1000)


def test_captures_events_only_for_target_date(tmp_path: Path) -> None:
    storage = tmp_path / "workspaceStorage"
    today = date(2026, 5, 7)
    local_tz = datetime.now().astimezone().tzinfo

    in_range = datetime.combine(today, datetime.min.time(), tzinfo=local_tz) + timedelta(hours=10)
    yesterday = in_range - timedelta(days=1)
    tomorrow = in_range + timedelta(days=1)

    _make_workspace(
        storage,
        "abc123",
        "file:///c%3A/Users/jason/projects/billable",
        [
            {
                "unixMs": _ms(in_range),
                "generationUUID": "gen-today",
                "type": "composer",
                "textDescription": "drafted the architecture plan",
            },
            {
                "unixMs": _ms(yesterday),
                "generationUUID": "gen-yesterday",
                "type": "composer",
                "textDescription": "yesterday's work",
            },
            {
                "unixMs": _ms(tomorrow),
                "generationUUID": "gen-tomorrow",
                "type": "composer",
                "textDescription": "tomorrow's work",
            },
        ],
    )

    events = CursorAdapter(storage_root=storage).capture(today)
    assert len(events) == 1
    e = events[0]
    assert e.source == "cursor"
    assert e.project_hint == "billable"
    assert "drafted the architecture plan" in e.content_excerpt
    assert e.artifact_ref == "cursor:abc123:gen-today"
    assert e.raw["type"] == "composer"


def test_skips_workspaces_without_db_or_generations(tmp_path: Path) -> None:
    storage = tmp_path / "workspaceStorage"
    storage.mkdir()
    # Workspace with no DB at all.
    (storage / "noresort").mkdir()
    # Workspace with empty DB.
    _make_workspace(storage, "empty", "file:///c%3A/x/foo", generations=None)
    # Workspace with empty list of generations.
    _make_workspace(storage, "emptylist", "file:///c%3A/x/bar", generations=[])

    events = CursorAdapter(storage_root=storage).capture(date(2026, 5, 7))
    assert events == []


def test_handles_missing_workspace_json(tmp_path: Path) -> None:
    storage = tmp_path / "workspaceStorage"
    today = date(2026, 5, 7)
    local_tz = datetime.now().astimezone().tzinfo
    when = datetime.combine(today, datetime.min.time(), tzinfo=local_tz) + timedelta(hours=12)

    _make_workspace(
        storage,
        "noref",
        folder_uri=None,  # workspace.json deliberately missing
        generations=[
            {
                "unixMs": _ms(when),
                "generationUUID": "g1",
                "type": "composer",
                "textDescription": "no project hint here",
            }
        ],
    )
    events = CursorAdapter(storage_root=storage).capture(today)
    assert len(events) == 1
    assert events[0].project_hint is None


def test_one_workspace_failure_does_not_kill_others(tmp_path: Path) -> None:
    storage = tmp_path / "workspaceStorage"
    today = date(2026, 5, 7)
    local_tz = datetime.now().astimezone().tzinfo
    when = datetime.combine(today, datetime.min.time(), tzinfo=local_tz) + timedelta(hours=10)

    # Bad workspace: state.vscdb is not a valid SQLite file.
    bad = storage / "bad"
    bad.mkdir(parents=True)
    (bad / "state.vscdb").write_bytes(b"not sqlite")
    (bad / "workspace.json").write_text(
        json.dumps({"folder": "file:///c%3A/x/bad"}), encoding="utf-8"
    )

    # Good workspace alongside it.
    _make_workspace(
        storage,
        "good",
        "file:///c%3A/x/good",
        [
            {
                "unixMs": _ms(when),
                "generationUUID": "g1",
                "type": "composer",
                "textDescription": "good event",
            }
        ],
    )

    events = CursorAdapter(storage_root=storage).capture(today)
    assert len(events) == 1
    assert events[0].project_hint == "good"


def test_sanitizes_secrets_in_excerpts(tmp_path: Path) -> None:
    storage = tmp_path / "workspaceStorage"
    today = date(2026, 5, 7)
    local_tz = datetime.now().astimezone().tzinfo
    when = datetime.combine(today, datetime.min.time(), tzinfo=local_tz) + timedelta(hours=10)

    leaked = "my key is sk-abc1234567890ABCDEF12345 here"
    _make_workspace(
        storage,
        "ws",
        "file:///c%3A/x/ws",
        [
            {
                "unixMs": _ms(when),
                "generationUUID": "g1",
                "type": "composer",
                "textDescription": leaked,
            }
        ],
    )
    events = CursorAdapter(storage_root=storage).capture(today)
    assert "sk-abc1234567890ABCDEF12345" not in events[0].content_excerpt
    assert "[REDACTED:openai_key]" in events[0].content_excerpt


def test_missing_storage_root_returns_empty(tmp_path: Path) -> None:
    events = CursorAdapter(storage_root=tmp_path / "does-not-exist").capture(date(2026, 5, 7))
    assert events == []
