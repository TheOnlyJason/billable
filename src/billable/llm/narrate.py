"""Stage 2 — write the billable narrative for each (date, matter) bucket.

Reads `prompts/narrate.md` for the system prompt. Sends one bucket at a time
so the model can focus and we can re-run a single bucket cheaply.

This is the high-leverage stage: it runs once per (date, matter) pair with
a stronger model. Output goes into `Entry.description`.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from billable.core.events import Session
from billable.llm.client import LLMClient, LLMError

log = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).resolve().parents[3] / "prompts" / "narrate.md"

# Maximum total content_excerpt characters we send per bucket. Keeps token
# cost bounded; the cluster stage already gave us a `summary_hint` per session
# so we don't strictly need every excerpt.
_MAX_EXCERPT_CHARS_PER_BUCKET = 8000


def _load_prompt() -> str:
    if not _PROMPT_PATH.exists():
        raise FileNotFoundError(f"Narrate prompt not found at {_PROMPT_PATH}")
    return _PROMPT_PATH.read_text(encoding="utf-8")


def _serialize_sessions(sessions: list[Session]) -> list[dict]:
    """Build the JSON payload for one (matter, day) bucket.

    Excerpts are budgeted across sessions: each session gets an even share
    of `_MAX_EXCERPT_CHARS_PER_BUCKET`, then truncated.
    """
    if not sessions:
        return []
    per_session_budget = _MAX_EXCERPT_CHARS_PER_BUCKET // len(sessions)

    out: list[dict] = []
    for s in sessions:
        excerpts: list[str] = []
        used = 0
        for ev in s.events:
            remaining = per_session_budget - used
            if remaining <= 0:
                excerpts.append("… [more events truncated]")
                break
            chunk = ev.content_excerpt[:remaining]
            excerpts.append(chunk)
            used += len(chunk)
        out.append(
            {
                "start": s.start.isoformat(),
                "end": s.end.isoformat(),
                "category": s.category,
                "summary_hint": s.summary_hint,
                "evidence": [ev.artifact_ref for ev in s.events],
                "event_excerpts": excerpts,
            }
        )
    return out


def narrate_bucket(
    *,
    date_iso: str,
    matter_id: str,
    matter_display_name: str,
    sessions: list[Session],
    llm: LLMClient,
    model: str,
) -> str:
    """Return the billable description for one (date, matter) bucket.

    `sessions` MUST all share the same matter_id and date. The caller (rollup)
    is responsible for grouping; this function does not re-validate.
    """
    if not sessions:
        return ""

    system = _load_prompt()
    payload = {
        "date": date_iso,
        "matter_id": matter_id,
        "matter_display_name": matter_display_name,
        "sessions": _serialize_sessions(sessions),
    }
    user = json.dumps(payload, ensure_ascii=False)

    response = llm.complete_json(system=system, user=user, model=model, temperature=0.3)
    description = response.get("description")
    if not isinstance(description, str) or not description.strip():
        raise LLMError(
            f"Narrate stage returned no description for matter {matter_id}. "
            f"Got: {response!r}"
        )
    return description.strip()
