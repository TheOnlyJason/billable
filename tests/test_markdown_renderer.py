"""Tests for MarkdownRenderer."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from billable.core.events import Entry
from billable.core.mapper import ProjectMapper, ProjectRule
from billable.renderers.markdown import MarkdownRenderer


def _entry(matter_id: str, description: str, hours: str, sources: list[str]) -> Entry:
    return Entry(
        date=date(2026, 5, 7),
        matter_id=matter_id,
        description=description,
        hours=Decimal(hours),
        sources=sources,
    )


def test_renders_table_with_total_and_audit(tmp_path: Path) -> None:
    mapper = ProjectMapper(
        rules=(
            ProjectRule(matter_id="acme", display_name="Acme Corp"),
            ProjectRule(matter_id="internal", display_name="Internal R&D"),
        )
    )
    entries = [
        _entry("acme", "Built the calendar component.", "2.00", ["cursor:abc:1", "cursor:abc:2"]),
        _entry("internal", "Drafted the architecture plan.", "1.25", ["cursor:def:1"]),
    ]

    renderer = MarkdownRenderer(mapper=mapper)
    out_path = renderer.render(target_date=date(2026, 5, 7), entries=entries, out_dir=tmp_path)

    text = out_path.read_text(encoding="utf-8")
    assert out_path.name == "2026-05-07.md"
    assert "# Timesheet — 2026-05-07" in text
    assert "| Matter | Description | Hours |" in text
    assert "| Acme Corp | Built the calendar component. | 2.00 |" in text
    assert "| Internal R&D | Drafted the architecture plan. | 1.25 |" in text
    assert "**Total: 3.25h**" in text
    assert "## Audit trail" in text
    assert "- `cursor:abc:1`" in text
    assert "- `cursor:abc:2`" in text


def test_empty_day_renders_friendly_placeholder(tmp_path: Path) -> None:
    renderer = MarkdownRenderer()
    out_path = renderer.render(target_date=date(2026, 5, 7), entries=[], out_dir=tmp_path)
    text = out_path.read_text(encoding="utf-8")
    assert "_No billable activity recorded for this day._" in text
    assert "## Audit trail" not in text  # nothing to audit


def test_pipe_in_description_is_escaped(tmp_path: Path) -> None:
    entries = [_entry("x", "Wrote a|b|c parser", "0.25", [])]
    renderer = MarkdownRenderer()
    out_path = renderer.render(target_date=date(2026, 5, 7), entries=entries, out_dir=tmp_path)
    text = out_path.read_text(encoding="utf-8")
    assert "Wrote a\\|b\\|c parser" in text


def test_newline_in_description_is_collapsed(tmp_path: Path) -> None:
    entries = [_entry("x", "Line one\nLine two", "0.25", [])]
    out_path = MarkdownRenderer().render(
        target_date=date(2026, 5, 7), entries=entries, out_dir=tmp_path
    )
    assert "Line one Line two" in out_path.read_text(encoding="utf-8")


def test_no_mapper_falls_back_to_matter_id(tmp_path: Path) -> None:
    entries = [_entry("acme", "x", "0.25", [])]
    out_path = MarkdownRenderer().render(
        target_date=date(2026, 5, 7), entries=entries, out_dir=tmp_path
    )
    text = out_path.read_text(encoding="utf-8")
    assert "| acme | x | 0.25 |" in text


# --- auto-classified surfacing (Layer 1) ------------------------------------


def test_auto_classified_matters_get_footer_and_label(tmp_path: Path) -> None:
    """Synthetic matters (no yaml rule) are tagged '(auto)' and listed in a footer."""
    mapper = ProjectMapper(
        rules=(ProjectRule(matter_id="acme", display_name="Acme Corp"),)
    )
    entries = [
        _entry("acme", "Built the calendar component.", "2.00", ["cursor:abc:1"]),
        _entry("bot-1", "Worked on Handshake bot.", "1.50", ["cursor:def:1"]),
    ]
    out_path = MarkdownRenderer(mapper=mapper).render(
        target_date=date(2026, 5, 7), entries=entries, out_dir=tmp_path
    )
    text = out_path.read_text(encoding="utf-8")

    assert "| Acme Corp | Built the calendar component." in text
    assert "(auto)" not in text.split("| Acme Corp")[0]
    assert "| Bot 1 (auto) | Worked on Handshake bot." in text
    assert "## Auto-classified matters" in text
    assert "`bot-1`" in text
    assert "(Bot 1)" in text


def test_no_auto_footer_when_all_entries_are_explicit(tmp_path: Path) -> None:
    mapper = ProjectMapper(
        rules=(ProjectRule(matter_id="acme", display_name="Acme Corp"),)
    )
    entries = [_entry("acme", "x", "1.00", [])]
    text = (
        MarkdownRenderer(mapper=mapper)
        .render(target_date=date(2026, 5, 7), entries=entries, out_dir=tmp_path)
        .read_text(encoding="utf-8")
    )
    assert "## Auto-classified matters" not in text
    assert "(auto)" not in text


def test_unclassified_does_not_count_as_auto(tmp_path: Path) -> None:
    """The synthetic 'unclassified' bucket is its own concept, not auto-discovery."""
    mapper = ProjectMapper(rules=())
    entries = [_entry("unclassified", "Something we couldn't place.", "0.50", [])]
    text = (
        MarkdownRenderer(mapper=mapper)
        .render(target_date=date(2026, 5, 7), entries=entries, out_dir=tmp_path)
        .read_text(encoding="utf-8")
    )
    assert "(auto)" not in text
    assert "## Auto-classified matters" not in text
