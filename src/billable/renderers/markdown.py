"""Markdown timesheet renderer.

Output convention (one file per day):

    # Timesheet — 2026-05-07

    | Matter | Description | Hours |
    | --- | --- | --- |
    | Internal R&D — Billable Agent | Drafted architecture plan ... | 1.25 |
    | Acme Corp — Website Redesign  | Built recurring-event ...     | 3.00 |

    **Total: 4.25h**

    ---

    ## Audit trail
    (artifact_refs grouped by matter, for spot-checking)

The Markdown is plain ASCII tables — no fancy formatting, no fences inside
descriptions — so it copy-pastes cleanly into email/Slack/Notion/Word.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from billable.core.events import Entry
from billable.core.mapper import ProjectMapper
from billable.renderers.base import Renderer


class MarkdownRenderer(Renderer):
    name = "markdown"
    extension = "md"

    def __init__(self, mapper: ProjectMapper | None = None) -> None:
        """`mapper` is used to look up display names for matters in the table.

        It is optional so the renderer can be used standalone (tests, etc.);
        when None, the matter_id itself is shown.
        """
        self.mapper = mapper

    def render(
        self,
        *,
        target_date: date,
        entries: list[Entry],
        out_dir: Path,
    ) -> Path:
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{target_date.isoformat()}.{self.extension}"
        out_path.write_text(self._build(target_date, entries), encoding="utf-8")
        return out_path

    # -- internals -----------------------------------------------------------

    def _build(self, target_date: date, entries: list[Entry]) -> str:
        lines: list[str] = [f"# Timesheet — {target_date.isoformat()}", ""]

        if not entries:
            lines.append("_No billable activity recorded for this day._")
            lines.append("")
            return "\n".join(lines)

        lines.append("| Matter | Description | Hours |")
        lines.append("| --- | --- | --- |")
        for entry in entries:
            display = (
                self.mapper.display_name(entry.matter_id) if self.mapper else entry.matter_id
            )
            lines.append(
                f"| {_escape_cell(display)} "
                f"| {_escape_cell(entry.description)} "
                f"| {_format_hours(entry.hours)} |"
            )

        total = sum((e.hours for e in entries), start=Decimal("0"))
        lines.append("")
        lines.append(f"**Total: {_format_hours(total)}h**")
        lines.append("")

        # Audit trail.
        lines.append("---")
        lines.append("")
        lines.append("## Audit trail")
        lines.append("")
        for entry in entries:
            display = (
                self.mapper.display_name(entry.matter_id) if self.mapper else entry.matter_id
            )
            lines.append(f"### {display}")
            if entry.sources:
                for ref in entry.sources:
                    lines.append(f"- `{ref}`")
            else:
                lines.append("- _(no sources recorded)_")
            lines.append("")

        return "\n".join(lines)


def _escape_cell(text: str) -> str:
    """Escape Markdown table cell content: collapse newlines, escape pipes."""
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ").strip()


def _format_hours(hours: Decimal) -> str:
    """Format hours with two decimal places (e.g. 1.25 -> '1.25')."""
    return f"{hours.quantize(Decimal('0.01')):.2f}"
