"""Tests for ProjectMapper — pure logic, no I/O except a tmpdir YAML round-trip."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from billable.core.events import Event
from billable.core.mapper import ProjectMapper, ProjectRule


def _ev(
    *,
    source: str = "cursor",
    project_hint: str | None = None,
    content_excerpt: str = "",
    artifact_ref: str = "ref",
    raw: dict | None = None,
) -> Event:
    return Event(
        timestamp=datetime(2026, 5, 7, 9, 0, 0),
        duration=timedelta(minutes=10),
        source=source,
        project_hint=project_hint,
        artifact_ref=artifact_ref,
        content_excerpt=content_excerpt,
        raw=raw or {},
    )


# --- ProjectRule.matches ----------------------------------------------------


def test_cursor_workspace_match_case_insensitive() -> None:
    rule = ProjectRule(
        matter_id="x", display_name="X", cursor_workspaces=("Billable", "acme-site")
    )
    assert rule.matches(_ev(source="cursor", project_hint="billable"))
    assert rule.matches(_ev(source="cursor", project_hint="ACME-Site"))
    assert not rule.matches(_ev(source="cursor", project_hint="other"))


def test_cursor_workspace_does_not_match_other_sources() -> None:
    rule = ProjectRule(matter_id="x", display_name="X", cursor_workspaces=("billable",))
    assert not rule.matches(_ev(source="gdocs", project_hint="billable"))


def test_gdoc_folder_id_match() -> None:
    rule = ProjectRule(matter_id="x", display_name="X", gdoc_folder_ids=("FOLDER1",))
    yes = _ev(source="gdocs", raw={"folder_ids": ["FOLDER0", "FOLDER1"]})
    no = _ev(source="gdocs", raw={"folder_ids": ["FOLDER2"]})
    missing = _ev(source="gdocs", raw={})
    assert rule.matches(yes)
    assert not rule.matches(no)
    assert not rule.matches(missing)


def test_gdoc_doc_id_match_via_raw_and_artifact_ref() -> None:
    rule = ProjectRule(matter_id="x", display_name="X", gdoc_doc_ids=("DOC1",))
    via_raw = _ev(source="gdocs", raw={"doc_id": "DOC1"}, artifact_ref="gdoc:OTHER")
    via_ref = _ev(source="gdocs", raw={}, artifact_ref="gdoc:DOC1")
    no_match = _ev(source="gdocs", raw={"doc_id": "DOC2"}, artifact_ref="gdoc:DOC2")
    assert rule.matches(via_raw)
    assert rule.matches(via_ref)
    assert not rule.matches(no_match)


def test_keywords_match_against_hint_and_excerpt_case_insensitive() -> None:
    rule = ProjectRule(matter_id="x", display_name="X", keywords=("Acme", "redesign"))
    assert rule.matches(_ev(project_hint="acme-site"))
    assert rule.matches(_ev(content_excerpt="Started the Redesign of the homepage"))
    assert not rule.matches(_ev(project_hint="other", content_excerpt="nothing relevant"))


def test_keyword_match_works_for_any_source() -> None:
    rule = ProjectRule(matter_id="x", display_name="X", keywords=("acme",))
    assert rule.matches(_ev(source="cursor", content_excerpt="acme stuff"))
    assert rule.matches(_ev(source="gdocs", content_excerpt="acme stuff"))


# --- ProjectMapper.resolve --------------------------------------------------


def test_first_rule_wins() -> None:
    specific = ProjectRule(
        matter_id="acme-site", display_name="Acme", cursor_workspaces=("acme",)
    )
    catchall = ProjectRule(matter_id="general", display_name="General", keywords=("a",))
    mapper = ProjectMapper(rules=(specific, catchall))
    assert mapper.resolve(_ev(project_hint="acme")) == "acme-site"
    assert mapper.resolve(_ev(project_hint="other", content_excerpt="aardvark")) == "general"


def test_resolve_returns_none_when_no_rule_matches() -> None:
    rule = ProjectRule(matter_id="x", display_name="X", cursor_workspaces=("billable",))
    mapper = ProjectMapper(rules=(rule,))
    assert mapper.resolve(_ev(project_hint="totally-unrelated")) is None


# --- display_name -----------------------------------------------------------


def test_display_name_lookup_and_fallbacks() -> None:
    mapper = ProjectMapper(
        rules=(ProjectRule(matter_id="acme", display_name="Acme Corp"),)
    )
    assert mapper.display_name("acme") == "Acme Corp"
    assert mapper.display_name("unclassified") == "Unclassified"
    assert mapper.display_name("unknown") == "unknown"


# --- from_yaml --------------------------------------------------------------


def test_from_yaml_round_trip(tmp_path: Path) -> None:
    yaml_path = tmp_path / "projects.yaml"
    yaml_path.write_text(
        """
projects:
  internal-billable-agent:
    display_name: "Internal R&D"
    cursor_workspaces: [billable]
    keywords: [billable agent]
  acme-website:
    display_name: "Acme Corp — Website"
    gdoc_folder_ids: [FOLDER1]
    keywords: [acme]
""",
        encoding="utf-8",
    )
    mapper = ProjectMapper.from_yaml(yaml_path)
    assert len(mapper.rules) == 2
    # Order preserved (first match wins).
    assert mapper.rules[0].matter_id == "internal-billable-agent"
    assert mapper.rules[1].matter_id == "acme-website"
    # display_name fallback.
    assert mapper.display_name("acme-website") == "Acme Corp — Website"


def test_from_yaml_missing_file_helpful_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="Copy config/projects.example.yaml"):
        ProjectMapper.from_yaml(tmp_path / "does-not-exist.yaml")


def test_from_yaml_empty_projects_section_is_ok(tmp_path: Path) -> None:
    yaml_path = tmp_path / "projects.yaml"
    yaml_path.write_text("projects: {}\n", encoding="utf-8")
    mapper = ProjectMapper.from_yaml(yaml_path)
    assert mapper.rules == ()
    assert mapper.resolve(_ev()) is None


def test_from_yaml_coerces_non_string_list_items_to_strings(tmp_path: Path) -> None:
    """Regression: bare YAML scalars like `1:1`, `2026`, `True` must not crash matchers.

    Without the coercion, `1:1` parses as the sexagesimal int 61 (YAML 1.1)
    and the keyword matcher dies with `'int' object has no attribute 'lower'`.
    """
    yaml_path = tmp_path / "projects.yaml"
    yaml_path.write_text(
        """
projects:
  admin:
    display_name: "Admin"
    keywords:
      - 1:1          # parses as int 61
      - 2026         # parses as int
      - "actual string"
""",
        encoding="utf-8",
    )
    mapper = ProjectMapper.from_yaml(yaml_path)
    rule = mapper.rules[0]
    assert all(isinstance(k, str) for k in rule.keywords)
    # Crucially: resolve() does not raise.
    assert mapper.resolve(_ev(content_excerpt="just some text")) is None
    assert mapper.resolve(_ev(content_excerpt="met for our 61 today")) == "admin"


def test_from_yaml_rejects_non_list_matcher(tmp_path: Path) -> None:
    yaml_path = tmp_path / "projects.yaml"
    yaml_path.write_text(
        """
projects:
  oops:
    display_name: "Oops"
    keywords: "should be a list"
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Expected a list"):
        ProjectMapper.from_yaml(yaml_path)
