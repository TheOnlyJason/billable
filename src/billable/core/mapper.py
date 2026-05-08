"""Project / matter mapping.

Reads `config/projects.yaml` and resolves each Event to a `matter_id`.
First match wins. No match → `None`, and the LLM is told to flag it.

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
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from billable.core.events import Event


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
            2. Otherwise, evaluate rules in YAML order; first match wins.
        """
        override = event.raw.get("matter_id")
        if isinstance(override, str) and override:
            return override
        for rule in self.rules:
            if rule.matches(event):
                return rule.matter_id
        return None

    def display_name(self, matter_id: str) -> str:
        """Human-readable name for a matter, e.g. 'Acme Corp — Website Redesign'.

        For the synthetic 'unclassified' matter, returns 'Unclassified'.
        """
        if matter_id == "unclassified":
            return "Unclassified"
        for rule in self.rules:
            if rule.matter_id == matter_id:
                return rule.display_name
        return matter_id


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
