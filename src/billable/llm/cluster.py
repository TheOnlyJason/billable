"""Stage 1 — cluster Events into Sessions.

Reads `prompts/cluster.md` for the system prompt. Sends events as a JSON
array in the user message. Parses the response into `Session` objects.

This is the cheap stage: it runs over many events with a small model.
See `prompts/cluster.md` for the contract.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from billable.core.events import Event, Session
from billable.llm.client import LLMClient, LLMError

log = logging.getLogger(__name__)

# The package layout puts prompts/ at the repo root, sibling to src/.
_PROMPT_PATH = Path(__file__).resolve().parents[3] / "prompts" / "cluster.md"

_VALID_CATEGORIES = {
    "planning",
    "building",
    "research",
    "meeting",
    "admin",
    "communication",
}


def _load_prompt() -> str:
    if not _PROMPT_PATH.exists():
        raise FileNotFoundError(f"Cluster prompt not found at {_PROMPT_PATH}")
    return _PROMPT_PATH.read_text(encoding="utf-8")


def _serialize_event_for_llm(event: Event, *, matter_id: str | None) -> dict:
    return {
        "timestamp": event.timestamp.isoformat(),
        "duration_minutes": (
            int(event.duration.total_seconds() / 60) if event.duration else None
        ),
        "source": event.source,
        "project_hint": event.project_hint,
        "matter_id": matter_id,
        "artifact_ref": event.artifact_ref,
        "content_excerpt": event.content_excerpt,
    }


def cluster_events(
    events: list[Event],
    *,
    llm: LLMClient,
    model: str,
    matter_resolver: Callable[[Event], str | None],
) -> list[Session]:
    """Group events into work sessions via the LLM.

    Returns sessions sorted by `start`. Sessions whose `matter_id` is None
    are still returned — the rollup stage decides what to do with them.

    `matter_resolver` is a callable `(Event) -> str | None` (typically
    `ProjectMapper.resolve`). We resolve here so the LLM sees the resolved
    matter and can use it for clustering.
    """
    if not events:
        return []

    system = _load_prompt()
    payload = [_serialize_event_for_llm(e, matter_id=matter_resolver(e)) for e in events]
    user = json.dumps({"events": payload}, ensure_ascii=False)

    response = llm.complete_json(system=system, user=user, model=model, temperature=0.1)

    raw_sessions = response.get("sessions")
    if not isinstance(raw_sessions, list):
        raise LLMError(
            f"Cluster stage returned unexpected shape: missing 'sessions' list. "
            f"Got top-level keys: {list(response.keys())}"
        )

    # Index events by artifact_ref so we can rebuild Sessions with real Event objects.
    by_ref = {e.artifact_ref: e for e in events}

    sessions: list[Session] = []
    for raw in raw_sessions:
        if not isinstance(raw, dict):
            continue
        try:
            start = datetime.fromisoformat(raw["start"])
            end = datetime.fromisoformat(raw["end"])
        except (KeyError, ValueError) as e:
            log.warning("Skipping session with bad timestamps: %s", e)
            continue

        category = raw.get("category", "building")
        if category not in _VALID_CATEGORIES:
            log.warning("Coercing unknown category %r to 'building'", category)
            category = "building"

        evidence_refs = raw.get("evidence") or []
        session_events = [by_ref[r] for r in evidence_refs if r in by_ref]

        sessions.append(
            Session(
                start=start,
                end=end,
                matter_id=raw.get("matter_id"),
                events=session_events,
                category=category,
                summary_hint=str(raw.get("summary_hint") or ""),
            )
        )

    if warnings := response.get("unclassified_warnings"):
        for w in warnings:
            log.info("Cluster warning: %s", w)

    sessions.sort(key=lambda s: s.start)
    return sessions
