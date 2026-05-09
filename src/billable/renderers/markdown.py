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
            lines.append(
                f"| {_escape_cell(self._matter_label(entry.matter_id))} "
                f"| {_escape_cell(entry.description)} "
                f"| {_format_hours(entry.hours)} |"
            )

        total = sum((e.hours for e in entries), start=Decimal("0"))
        lines.append("")
        lines.append(f"**Total: {_format_hours(total)}h**")
        lines.append("")

        auto_entries = [e for e in entries if self._is_auto(e.matter_id)]
        if auto_entries:
            lines.append("---")
            lines.append("")
            lines.append("## Auto-classified matters")
            lines.append("")
            lines.append(
                "These matters were inferred from your Cursor workspace folders "
                "with no explicit rule in `config/projects.yaml`. To customize "
                "the display name, group multiple workspaces, or set billing "
                "metadata, run `billable discover` or add a rule by hand."
            )
            lines.append("")
            for entry in auto_entries:
                lines.append(
                    f"- `{entry.matter_id}` "
                    f"({self.mapper.display_name(entry.matter_id) if self.mapper else entry.matter_id})"
                    f" — {_format_hours(entry.hours)}h"
                )
            lines.append("")

        lines.append("---")
        lines.append("")
        lines.append("## Audit trail")
        lines.append("")
        for entry in entries:
            lines.append(f"### {self._matter_label(entry.matter_id)}")
            if entry.sources:
                for ref in entry.sources:
                    lines.append(f"- `{ref}`")
            else:
                lines.append("- _(no sources recorded)_")
            lines.append("")

        return "\n".join(lines)

    def _matter_label(self, matter_id: str) -> str:
        display = (
            self.mapper.display_name(matter_id) if self.mapper else matter_id
        )
        if self._is_auto(matter_id):
            return f"{display} (auto)"
        return display

    def _is_auto(self, matter_id: str) -> bool:
        if matter_id == "unclassified" or self.mapper is None:
            return False
        return not self.mapper.is_explicit(matter_id)


def _escape_cell(text: str) -> str:
    """Escape Markdown table cell content: collapse newlines, escape pipes."""
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ").strip()


def _format_hours(hours: Decimal) -> str:
    """Format hours with two decimal places (e.g. 1.25 -> '1.25')."""
    return f"{hours.quantize(Decimal('0.01')):.2f}"
