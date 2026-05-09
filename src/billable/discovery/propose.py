"""Turn a `DiscoveryScan` into a draft `projects.yaml`.

Pipeline:

    DiscoveryScan -> JSON payload -> LLM (JSON-mode) -> validated -> YAML string

The LLM call is the only side effect; the YAML rendering is deterministic
and side-effect-free so the CLI can show a clean diff to the user before
anything is written to disk.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from billable.discovery.scan import DiscoveryScan
from billable.llm.client import LLMClient, LLMError

log = logging.getLogger(__name__)

DEFAULT_PROMPT_PATH = Path(__file__).parent.parent.parent.parent / "prompts" / "discover.md"


@dataclass(frozen=True)
class ProposalResult:
    """What `propose_projects_yaml` returns."""

    yaml_text: str
    project_count: int
    skipped_patterns: tuple[dict, ...]
    raw_llm_payload: dict


def propose_projects_yaml(
    scan: DiscoveryScan,
    *,
    llm: LLMClient,
    model: str = "gpt-4o-mini",
    prompt_path: Path | None = None,
) -> ProposalResult:
    """Ask the LLM to draft a projects.yaml from a discovery scan.

    Returns the YAML as a string plus metadata about the proposal.
    Raises `LLMError` if the LLM returns malformed output that we can't
    repair.
    """
    if not scan.cursor_workspaces and not scan.browser_patterns:
        raise ValueError(
            "Empty scan — no Cursor workspaces and no AW patterns. "
            "Open a Cursor workspace and use it for at least one prompt, "
            "then run `billable discover` again."
        )

    prompt_path = prompt_path or DEFAULT_PROMPT_PATH
    system_prompt = _load_system_prompt(prompt_path)
    user_payload = json.dumps(scan.to_dict(), indent=2)

    response = llm.complete_json(
        system=system_prompt,
        user=user_payload,
        model=model,
        temperature=0.3,
    )

    projects = _validate_projects(response)
    yaml_text = _render_yaml(projects)
    skipped = tuple(response.get("skipped_browser_patterns") or [])

    return ProposalResult(
        yaml_text=yaml_text,
        project_count=len(projects),
        skipped_patterns=skipped,
        raw_llm_payload=response,
    )


# --- internals --------------------------------------------------------------


def _load_system_prompt(path: Path) -> str:
    if not path.exists():
        raise LLMError(f"Discover prompt not found at {path}")
    return path.read_text(encoding="utf-8")


def _validate_projects(payload: Any) -> list[dict]:
    """Strict-ish validation of the LLM's JSON shape.

    We tolerate a couple of variations (`projects` as list or dict; missing
    `keywords` field) but reject anything that wouldn't load cleanly into
    `ProjectMapper.from_yaml`.
    """
    if not isinstance(payload, dict):
        raise LLMError(f"Discover LLM did not return a JSON object: {type(payload).__name__}")

    raw_projects = payload.get("projects")
    if raw_projects is None:
        raise LLMError("Discover LLM response is missing 'projects' field.")

    items: list[dict]
    if isinstance(raw_projects, list):
        items = raw_projects
    elif isinstance(raw_projects, dict):
        # Tolerant: allow dict-shape too, where each key is the matter_id.
        items = []
        for matter_id, body in raw_projects.items():
            if not isinstance(body, dict):
                continue
            items.append({**body, "matter_id": matter_id})
    else:
        raise LLMError(
            f"Discover LLM 'projects' must be a list or object, got {type(raw_projects).__name__}"
        )

    cleaned: list[dict] = []
    seen_ids: set[str] = set()
    for raw in items:
        if not isinstance(raw, dict):
            continue
        matter_id = str(raw.get("matter_id") or "").strip().lower()
        if not matter_id or not _is_valid_matter_id(matter_id):
            log.warning("Discover LLM dropped entry with invalid matter_id: %r", raw.get("matter_id"))
            continue
        if matter_id in seen_ids:
            log.warning("Discover LLM emitted duplicate matter_id %r; keeping first", matter_id)
            continue
        seen_ids.add(matter_id)
        cleaned.append(
            {
                "matter_id": matter_id,
                "display_name": str(raw.get("display_name") or matter_id),
                "cursor_workspaces": _coerce_str_list(raw.get("cursor_workspaces")),
                "keywords": _coerce_str_list(raw.get("keywords")),
                "rationale": str(raw.get("rationale") or "").strip(),
            }
        )

    if not cleaned:
        raise LLMError("Discover LLM returned zero usable projects after validation.")
    return cleaned


def _is_valid_matter_id(s: str) -> bool:
    return all(c.isalnum() or c in "-_" for c in s) and len(s) <= 64


def _coerce_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        s = str(item).strip()
        if s:
            out.append(s)
    return out


def _render_yaml(projects: list[dict]) -> str:
    """Render the validated proposal as a YAML string with a header.

    Hand-formatted (rather than `yaml.safe_dump`) so the output is
    consistent and human-friendly: comments, blank lines, double-quoted
    display names that contain the em-dash safely.
    """
    lines: list[str] = [
        "# Auto-generated by `billable discover`.",
        "# Review this file, edit display names, and overwrite",
        "# config/projects.yaml when you're happy with it.",
        "#",
        "# Resolution order:",
        "#   1. Hard `--matter` override on a note",
        "#   2. Rules below in YAML order (first match wins)",
        "#   3. Auto-discovery: any unmatched cursor/AW event with a",
        "#      project_hint synthesizes a matter from the hint itself.",
        "",
        "projects:",
    ]
    for i, p in enumerate(projects):
        if i > 0:
            lines.append("")
        if p.get("rationale"):
            lines.append(f"  # {p['rationale']}")
        lines.append(f"  {p['matter_id']}:")
        lines.append(f"    display_name: {_yaml_string(p['display_name'])}")
        if p["cursor_workspaces"]:
            lines.append(f"    cursor_workspaces: {_yaml_inline_list(p['cursor_workspaces'])}")
        if p["keywords"]:
            lines.append(f"    keywords: {_yaml_inline_list(p['keywords'])}")
    lines.append("")
    return "\n".join(lines)


def _yaml_string(s: str) -> str:
    """Always double-quote so non-ASCII (em-dash, accents) is safe."""
    escaped = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _yaml_inline_list(items: list[str]) -> str:
    """Render a short string list inline: ``[a, b, c]``.

    Quotes individual items only if they contain whitespace, ``:``, or
    other YAML-significant characters.
    """
    parts: list[str] = []
    for item in items:
        if any(c in item for c in " :,#[]{}\"'") or not item:
            parts.append(_yaml_string(item))
        else:
            parts.append(item)
    return "[" + ", ".join(parts) + "]"
