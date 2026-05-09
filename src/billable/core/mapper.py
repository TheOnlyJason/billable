"""Project / matter mapping.

Reads `config/projects.yaml` and resolves each Event to a `matter_id`.
First match wins. If no rule matches but the event carries a stable
`project_hint` (Cursor workspace folder, AW-extracted Cursor workspace),
the mapper synthesizes a matter from the hint — so a brand-new project
shows up in the report on day one without any yaml editing.

See `config/projects.example.yaml` for the schema.

Matcher semantics (logical OR within a project, logical AND within a matcher):

    cursor_workspaces  fires if event.source == "cursor"
                       AND event.project_hint is in the list (case-insensitive)

    gdoc_folder_ids    fires if event.source == "gdocs"
                       AND any folder id in event.raw["folder_ids"] is in the list

    gdoc_doc_ids       fires if event.source == "gdocs"
                       AND event.raw.get("doc_id") is in the list
                            OR event.artifact_ref == f"gdoc:<doc_id>" matches

    keywords           fires for ANY source if any keyword (case-insensitive)
                       is a substring of project_hint OR content_excerpt

Auto-discovery fallback (after all rules have been tried):

    For events whose source is `cursor` or `activitywatch` AND whose
    `project_hint` is set, the mapper synthesizes
    matter_id = slugify(project_hint). The display name is humanized
    from the same hint ("bot-1" -> "Bot 1"). Use `is_explicit(matter_id)`
    to tell auto-discovered matters apart from yaml-declared ones.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from billable.core.events import Event

# Sources whose `project_hint` is a stable, human-readable identifier
# (a folder name) that we can safely promote to a synthetic matter_id.
# Other sources (gdocs, notes) have noisier hints (doc title, none) and
# stay Unclassified unless an explicit rule or override matches.
_AUTO_DISCOVER_SOURCES: frozenset[str] = frozenset({"cursor", "activitywatch"})


@dataclass(frozen=True)
class ProjectRule:
    matter_id: str
    display_name: str
    cursor_workspaces: tuple[str, ...] = ()
    gdoc_folder_ids: tuple[str, ...] = ()
    gdoc_doc_ids: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()

    def matches(self, event: Event) -> bool:
        # cursor_workspaces
        if event.source == "cursor" and self.cursor_workspaces:
            hint = (event.project_hint or "").lower()
            if hint and hint in {w.lower() for w in self.cursor_workspaces}:
                return True

        # gdoc_folder_ids
        if event.source == "gdocs" and self.gdoc_folder_ids:
            ancestor_ids = event.raw.get("folder_ids") or []
            if isinstance(ancestor_ids, list) and set(ancestor_ids) & set(self.gdoc_folder_ids):
                return True

        # gdoc_doc_ids
        if event.source == "gdocs" and self.gdoc_doc_ids:
            doc_id = event.raw.get("doc_id")
            if doc_id and doc_id in self.gdoc_doc_ids:
                return True
            for did in self.gdoc_doc_ids:
                if event.artifact_ref == f"gdoc:{did}":
                    return True

        # keywords (any source)
        if self.keywords:
            haystack = " ".join(
                filter(None, [event.project_hint or "", event.content_excerpt or ""])
            ).lower()
            for kw in self.keywords:
                if kw.lower() in haystack:
                    return True

        return False


@dataclass(frozen=True)
class ProjectMapper:
    rules: tuple[ProjectRule, ...] = field(default_factory=tuple)

    @classmethod
    def from_yaml(cls, path: Path) -> ProjectMapper:
        """Load rules from `config/projects.yaml`.

        Order in the YAML file is preserved. The first matching rule wins.
        """
        if not path.exists():
            raise FileNotFoundError(
                f"Project config not found at {path}. "
                f"Copy config/projects.example.yaml to {path} and edit it."
            )
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        projects = data.get("projects") or {}
        if not isinstance(projects, dict):
            raise ValueError(
                f"{path}: top-level 'projects' must be a mapping of matter_id -> rule."
            )

        rules: list[ProjectRule] = []
        for matter_id, body in projects.items():
            body = body or {}
            rules.append(
                ProjectRule(
                    matter_id=str(matter_id),
                    display_name=str(body.get("display_name") or matter_id),
                    # Coerce every list element to str. YAML happily parses bare
                    # values like `1:1`, `2026`, or `True` as int/bool/etc; the
                    # matchers all operate on strings, so normalize here.
                    cursor_workspaces=_to_str_tuple(body.get("cursor_workspaces")),
                    gdoc_folder_ids=_to_str_tuple(body.get("gdoc_folder_ids")),
                    gdoc_doc_ids=_to_str_tuple(body.get("gdoc_doc_ids")),
                    keywords=_to_str_tuple(body.get("keywords")),
                )
            )
        return cls(rules=tuple(rules))

    def resolve(self, event: Event) -> str | None:
        """Return the matter_id for this event, or None if unclassified.

        Resolution order:
            1. Hard override: if the event sets ``raw['matter_id']`` (e.g. a
               hotkey note with ``--matter``), trust it. Adapters use this
               to assert ground truth that bypasses keyword/folder rules.
            2. Evaluate rules in YAML order; first match wins.
            3. Auto-discovery fallback: for ``cursor`` / ``activitywatch``
               events with a ``project_hint``, synthesize a matter from
               the hint (lowercased + slugified). This is what makes the
               tool work on a fresh install with an empty projects.yaml.
        """
        override = event.raw.get("matter_id")
        if isinstance(override, str) and override:
            return override
        for rule in self.rules:
            if rule.matches(event):
                return rule.matter_id
        if event.source in _AUTO_DISCOVER_SOURCES and event.project_hint:
            return slugify_matter_id(event.project_hint)
        return None

    def is_explicit(self, matter_id: str) -> bool:
        """True if this matter_id has an explicit rule in projects.yaml.

        Used by the renderer to mark auto-discovered matters in the report
        so the user can promote them to real entries when they're ready.
        """
        return any(rule.matter_id == matter_id for rule in self.rules)

    def display_name(self, matter_id: str) -> str:
        """Human-readable name for a matter, e.g. 'Acme Corp — Website Redesign'.

        For yaml-declared matters, returns the configured ``display_name``.
        For auto-discovered matters (no rule), humanizes the matter_id
        ("bot-1" -> "Bot 1", "student-tuition-portal" -> "Student Tuition Portal").
        For the synthetic 'unclassified' matter, returns 'Unclassified'.
        """
        if matter_id == "unclassified":
            return "Unclassified"
        for rule in self.rules:
            if rule.matter_id == matter_id:
                return rule.display_name
        return humanize_matter_id(matter_id)


def slugify_matter_id(value: str) -> str:
    """Normalize a project_hint into a stable matter_id.

    Lowercases, replaces runs of whitespace and ``[/\\.,]`` with a single
    hyphen, strips leading/trailing hyphens. Keeps existing hyphens and
    underscores so folder names like ``bot-1`` and
    ``student_tuition_portal`` round-trip cleanly.

    Returns the original (lowercased) string if normalization would
    produce an empty result.
    """
    s = value.strip().lower()
    if not s:
        return s
    s = re.sub(r"[\s/\\.,]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s or value.strip().lower()


def humanize_matter_id(matter_id: str) -> str:
    """Turn an auto-discovered matter_id into a friendly display name.

    Examples:
        ``bot-1`` -> ``Bot 1``
        ``student-tuition-payment-portal`` -> ``Student Tuition Payment Portal``
        ``my_cool_app`` -> ``My Cool App``
    """
    if not matter_id:
        return matter_id
    spaced = re.sub(r"[-_]+", " ", matter_id).strip()
    spaced = re.sub(r"\s+", " ", spaced)
    return " ".join(w.capitalize() if not w.isupper() else w for w in spaced.split(" "))


def _to_str_tuple(value: object) -> tuple[str, ...]:
    """Coerce a YAML list value to a tuple of strings.

    Tolerates None (missing key), accepts any scalar that has a string form.
    Filters out empty strings so a stray ``- ""`` in the YAML doesn't
    silently match every event.
    """
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(
            f"Expected a list in projects.yaml, got {type(value).__name__}: {value!r}"
        )
    out: list[str] = []
    for item in value:
        s = str(item).strip()
        if s:
            out.append(s)
    return tuple(out)
