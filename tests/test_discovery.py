"""Tests for the discovery layer (scan + propose).

The LLM call is mocked. The scanning layer is tested against in-memory
adapter fakes so no Cursor or ActivityWatch install is required.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from billable.core.events import Event
from billable.core.mapper import ProjectMapper
from billable.discovery.propose import (
    _coerce_str_list,
    _is_valid_matter_id,
    _render_yaml,
    _validate_projects,
    propose_projects_yaml,
)
from billable.discovery.scan import (
    BrowserPattern,
    CursorWorkspace,
    DiscoveryScan,
    _pick_sample_prompts,
    _scan_browser,
    _scan_cursor,
    _title_prefix,
)
from billable.llm.client import LLMClient, LLMError


# --- fake adapters ---------------------------------------------------------


class _FakeCursor:
    def __init__(self, events_by_day: dict[date, list[Event]]) -> None:
        self._events = events_by_day

    def capture(self, target_date: date) -> list[Event]:
        return list(self._events.get(target_date, []))


class _FakeAW:
    def __init__(self, events_by_day: dict[date, list[Event]], reachable: bool = True) -> None:
        self._events = events_by_day
        self._reachable = reachable

    def capture(self, target_date: date) -> list[Event]:
        return list(self._events.get(target_date, []))

    def _list_buckets(self) -> dict[str, Any]:
        if not self._reachable:
            from billable.adapters.activitywatch import _AWUnavailable

            raise _AWUnavailable("not reachable in test")
        return {"aw-watcher-window_test": {}}


def _cursor_event(ts: datetime, workspace: str, prompt: str) -> Event:
    return Event(
        timestamp=ts,
        duration=None,
        source="cursor",
        project_hint=workspace,
        artifact_ref=f"cursor:{workspace}:{ts.isoformat()}",
        content_excerpt=prompt,
    )


def _aw_event(ts: datetime, app: str, title: str, minutes: int) -> Event:
    return Event(
        timestamp=ts,
        duration=timedelta(minutes=minutes),
        source="activitywatch",
        project_hint=None,
        artifact_ref=f"aw:{ts.isoformat()}:{app}",
        content_excerpt=f"Focused on '{title}' in {app}.",
        raw={"app": app, "title": title},
    )


# --- _title_prefix ---------------------------------------------------------


def test_title_prefix_strips_and_n_more_pages() -> None:
    assert (
        _title_prefix("AIBuddy and 15 more pages - Profile 1 - Microsoft Edge")
        == "AIBuddy - Profile"
    )
    assert (
        _title_prefix("ChatGPT and 7 more pages - Profile 1 - Microsoft Edge")
        == "ChatGPT - Profile"
    )


def test_title_prefix_keeps_first_three_words() -> None:
    assert _title_prefix("a b c d e f") == "a b c"


def test_title_prefix_handles_short_titles() -> None:
    assert _title_prefix("LinkedIn") == "LinkedIn"


# --- _pick_sample_prompts --------------------------------------------------


def test_sample_prompts_drops_near_duplicates_and_caps() -> None:
    prompts = [
        "this is a really substantive prompt about feature X",
        "this is a really substantive prompt about feature X (again)",
        "different prompt about feature Y",
        "yet another distinct prompt body",
        "fifth different topic entirely so we have variety",
        "sixth different topic so we exceed the cap",
    ]
    samples = _pick_sample_prompts(prompts)
    # Cap is 5; near-duplicates collapse to one entry.
    assert len(samples) <= 5
    # First-60-char prefixes are unique.
    prefixes = {s[:60].lower() for s in samples}
    assert len(prefixes) == len(samples)


def test_sample_prompts_handles_empty_input() -> None:
    assert _pick_sample_prompts([]) == ()


# --- _scan_cursor ----------------------------------------------------------


def test_scan_cursor_groups_by_workspace_and_ranks_by_count() -> None:
    today = date(2026, 5, 8)
    tz = timezone.utc
    events = {
        today: [
            _cursor_event(datetime(2026, 5, 8, 9, 0, tzinfo=tz), "billable", "p1"),
            _cursor_event(datetime(2026, 5, 8, 10, 0, tzinfo=tz), "billable", "p2"),
            _cursor_event(datetime(2026, 5, 8, 11, 0, tzinfo=tz), "bot-1", "p3"),
        ],
        today - timedelta(days=1): [
            _cursor_event(datetime(2026, 5, 7, 9, 0, tzinfo=tz), "billable", "p4"),
        ],
    }
    workspaces = _scan_cursor(_FakeCursor(events), days=7, today=today)

    by_name = {w.folder_name: w for w in workspaces}
    assert by_name["billable"].prompt_count == 3
    assert by_name["bot-1"].prompt_count == 1
    # Ranked by count descending.
    assert workspaces[0].folder_name == "billable"
    # Last-seen is the most recent timestamp seen.
    assert by_name["billable"].last_seen.day == 8


def test_scan_cursor_ignores_events_without_workspace() -> None:
    today = date(2026, 5, 8)
    tz = timezone.utc
    events = {
        today: [
            Event(  # no project_hint
                timestamp=datetime(2026, 5, 8, 9, 0, tzinfo=tz),
                duration=None,
                source="cursor",
                project_hint=None,
                artifact_ref="x",
                content_excerpt="orphan",
            ),
        ],
    }
    assert _scan_cursor(_FakeCursor(events), days=1, today=today) == ()


# --- _scan_browser ---------------------------------------------------------


def test_scan_browser_excludes_cursor_windows() -> None:
    today = date(2026, 5, 8)
    tz = timezone.utc
    events = {
        today: [
            # Cursor window — must be excluded (handled by cursor scan).
            _aw_event(
                datetime(2026, 5, 8, 9, 0, tzinfo=tz),
                "Cursor.exe",
                ".env - bot-1 - Cursor",
                10,
            ),
            # Browser window — should be included.
            _aw_event(
                datetime(2026, 5, 8, 10, 0, tzinfo=tz),
                "msedge.exe",
                "AIBuddy and 15 more pages - Profile 1 - Microsoft Edge",
                28,
            ),
        ],
    }
    available, patterns = _scan_browser(_FakeAW(events), days=1, today=today)
    assert available is True
    assert len(patterns) == 1
    assert patterns[0].app == "msedge.exe"
    assert "AIBuddy" in patterns[0].title_prefix


def test_scan_browser_groups_variants_under_one_pattern() -> None:
    today = date(2026, 5, 8)
    tz = timezone.utc
    events = {
        today: [
            _aw_event(
                datetime(2026, 5, 8, 9, 0, tzinfo=tz),
                "msedge.exe",
                "AIBuddy and 15 more pages - Profile 1 - Microsoft Edge",
                10,
            ),
            _aw_event(
                datetime(2026, 5, 8, 10, 0, tzinfo=tz),
                "msedge.exe",
                "AIBuddy and 7 more pages - Profile 1 - Microsoft Edge",
                15,
            ),
        ],
    }
    _, patterns = _scan_browser(_FakeAW(events), days=1, today=today)
    assert len(patterns) == 1
    assert patterns[0].occurrence_count == 2
    assert patterns[0].total_minutes == 25


def test_scan_browser_marks_unreachable() -> None:
    available, patterns = _scan_browser(
        _FakeAW({}, reachable=False), days=1, today=date(2026, 5, 8)
    )
    assert available is False
    assert patterns == ()


# --- propose validation ----------------------------------------------------


def test_validate_projects_accepts_well_formed_payload() -> None:
    payload = {
        "projects": [
            {
                "matter_id": "billable",
                "display_name": "Billable",
                "cursor_workspaces": ["billable"],
                "keywords": ["billable", "timesheet"],
                "rationale": "obvious",
            }
        ]
    }
    out = _validate_projects(payload)
    assert out[0]["matter_id"] == "billable"
    assert out[0]["cursor_workspaces"] == ["billable"]


def test_validate_projects_tolerates_dict_shape() -> None:
    payload = {
        "projects": {
            "billable": {
                "display_name": "Billable",
                "keywords": ["billable"],
            }
        }
    }
    out = _validate_projects(payload)
    assert out[0]["matter_id"] == "billable"


def test_validate_projects_rejects_missing_projects_key() -> None:
    with pytest.raises(LLMError, match="missing 'projects'"):
        _validate_projects({"foo": "bar"})


def test_validate_projects_rejects_zero_usable_results() -> None:
    payload = {
        "projects": [
            {"matter_id": "INVALID space", "display_name": "x"},
            {"matter_id": ""},
        ]
    }
    with pytest.raises(LLMError, match="zero usable"):
        _validate_projects(payload)


def test_validate_projects_dedupes_matter_ids() -> None:
    payload = {
        "projects": [
            {"matter_id": "x", "display_name": "X first"},
            {"matter_id": "x", "display_name": "X second"},
        ]
    }
    out = _validate_projects(payload)
    assert len(out) == 1
    assert out[0]["display_name"] == "X first"


def test_is_valid_matter_id() -> None:
    assert _is_valid_matter_id("acme")
    assert _is_valid_matter_id("acme-website")
    assert _is_valid_matter_id("bot_1")
    assert _is_valid_matter_id("a")
    assert not _is_valid_matter_id("has space")
    assert not _is_valid_matter_id("with/slash")
    assert not _is_valid_matter_id("a" * 65)  # too long


def test_coerce_str_list_handles_variants() -> None:
    assert _coerce_str_list(None) == []
    assert _coerce_str_list("solo") == ["solo"]
    assert _coerce_str_list(["a", "b", "  ", "c"]) == ["a", "b", "c"]
    assert _coerce_str_list([1, 2, 3]) == ["1", "2", "3"]


# --- YAML rendering --------------------------------------------------------


def test_render_yaml_round_trips_through_mapper(tmp_path: Path) -> None:
    """The YAML we generate must load cleanly into ProjectMapper."""
    projects = [
        {
            "matter_id": "internal-billable-agent",
            "display_name": "Billable Agent",
            "cursor_workspaces": ["billable"],
            "keywords": ["billable agent", "timesheet ai"],
            "rationale": "Internal R&D project.",
        },
        {
            "matter_id": "acme-website",
            "display_name": "Acme — Website",
            "cursor_workspaces": ["acme-site"],
            "keywords": ["acme"],
            "rationale": "Client work for Acme.",
        },
    ]
    text = _render_yaml(projects)
    yaml_path = tmp_path / "projects.yaml"
    yaml_path.write_text(text, encoding="utf-8")

    mapper = ProjectMapper.from_yaml(yaml_path)
    assert len(mapper.rules) == 2
    assert mapper.rules[0].matter_id == "internal-billable-agent"
    assert mapper.rules[1].display_name == "Acme — Website"
    assert "billable" in mapper.rules[0].cursor_workspaces


def test_render_yaml_includes_rationale_as_comment() -> None:
    projects = [
        {
            "matter_id": "x",
            "display_name": "X",
            "cursor_workspaces": [],
            "keywords": [],
            "rationale": "because reasons",
        }
    ]
    text = _render_yaml(projects)
    assert "# because reasons" in text
    assert text.startswith("# Auto-generated by `billable discover`")


def test_render_yaml_skips_empty_field_lines() -> None:
    projects = [
        {
            "matter_id": "x",
            "display_name": "X",
            "cursor_workspaces": [],
            "keywords": [],
            "rationale": "",
        }
    ]
    text = _render_yaml(projects)
    assert "cursor_workspaces:" not in text
    assert "keywords:" not in text


def test_render_yaml_quotes_problematic_keywords() -> None:
    projects = [
        {
            "matter_id": "admin",
            "display_name": "Admin",
            "cursor_workspaces": [],
            "keywords": ["1:1", "with space", "ok"],
            "rationale": "",
        }
    ]
    text = _render_yaml(projects)
    assert '"1:1"' in text
    assert '"with space"' in text
    assert " ok" in text  # unquoted is fine


# --- propose_projects_yaml end-to-end (mocked LLM) -------------------------


class _FakeLLM(LLMClient):
    def __init__(self, response: dict) -> None:
        self._response = response
        self.last_call: dict | None = None

    def complete_json(self, *, system: str, user: str, model: str, temperature: float = 0.2) -> dict:
        self.last_call = {"system": system, "user": user, "model": model}
        return self._response


def _scan_with_one_workspace() -> DiscoveryScan:
    return DiscoveryScan(
        days_scanned=7,
        cursor_workspaces=(
            CursorWorkspace(
                folder_name="billable",
                prompt_count=10,
                last_seen=datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc),
                sample_prompts=("Built the activity tracker", "Wrote the rollup logic"),
            ),
        ),
        browser_patterns=(
            BrowserPattern(
                app="msedge.exe",
                title_prefix="AIBuddy",
                occurrence_count=6,
                total_minutes=28,
                example_titles=("AIBuddy and 15 more pages",),
            ),
        ),
        note_count=4,
        activitywatch_available=True,
    )


def test_propose_projects_yaml_full_flow() -> None:
    scan = _scan_with_one_workspace()
    llm = _FakeLLM(
        response={
            "projects": [
                {
                    "matter_id": "billable",
                    "display_name": "Billable Time Agent",
                    "cursor_workspaces": ["billable"],
                    "keywords": ["billable", "activity tracker"],
                    "rationale": "Workspace 'billable' with prompts about an activity tracker.",
                },
                {
                    "matter_id": "aibuddy",
                    "display_name": "AIBuddy",
                    "cursor_workspaces": [],
                    "keywords": ["AIBuddy"],
                    "rationale": "Recurring browser pattern.",
                },
            ],
            "skipped_browser_patterns": [
                {"title_prefix": "LinkedIn", "reason": "Personal."}
            ],
        }
    )
    result = propose_projects_yaml(scan, llm=llm, model="test-model")
    assert result.project_count == 2
    assert "billable:" in result.yaml_text
    assert "aibuddy:" in result.yaml_text
    assert len(result.skipped_patterns) == 1
    # Confirm the scan was JSON-serialized into the user prompt.
    assert llm.last_call is not None
    assert "billable" in llm.last_call["user"]
    assert "AIBuddy" in llm.last_call["user"]


def test_propose_rejects_empty_scan() -> None:
    empty = DiscoveryScan(days_scanned=7)
    with pytest.raises(ValueError, match="Empty scan"):
        propose_projects_yaml(empty, llm=_FakeLLM({}), model="test")


def test_propose_propagates_llm_errors() -> None:
    scan = _scan_with_one_workspace()
    llm = _FakeLLM(response={"oops": "no projects key"})
    with pytest.raises(LLMError, match="missing 'projects'"):
        propose_projects_yaml(scan, llm=llm, model="test")
