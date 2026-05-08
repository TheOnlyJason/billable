"""Top-level orchestration.

The pipeline composes adapters, mapper, LLM stages, rollup, and renderers.
Each step is pluggable so tests and the v2 desktop agent can swap pieces
without touching this file.

Flow (see DESIGN.md §3 for the full diagram):

    capture (per adapter)
        -> cache to disk (event-sourced; lets us re-run later stages cheaply)
        -> LLM stage 1: cluster events into Sessions
        -> cache sessions to disk (lets us re-run only narrate)
        -> LLM stage 2 + rollup: narrate per (matter, day), apply policy
        -> render via the chosen renderer

The cache layout under `cache_dir`:

    events/<date>.json     # raw events from all adapters (after sanitize)
    sessions/<date>.json   # output of stage 1
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal

from billable.adapters.base import CaptureAdapter
from billable.core.events import Entry, Event, Session
from billable.core.mapper import ProjectMapper
from billable.llm.client import LLMClient
from billable.llm.cluster import cluster_events
from billable.renderers.base import Renderer
from billable.rollup.daily import BillingPolicy, rollup_day

log = logging.getLogger(__name__)

Stage = Literal["cluster", "narrate"]


@dataclass
class PipelineConfig:
    target_date: date
    adapters: list[CaptureAdapter]
    mapper: ProjectMapper
    llm: LLMClient
    cluster_model: str
    narrate_model: str
    renderer: Renderer
    policy: BillingPolicy
    cache_dir: Path
    out_dir: Path
    reuse_cache: bool = False
    skip_llm: bool = False
    only_stage: Stage | None = None


class Pipeline:
    def __init__(self, config: PipelineConfig) -> None:
        self.config = config

    def run(self) -> list[Entry]:
        """Execute the full pipeline for `config.target_date`.

        Returns the list of rendered entries. The renderer side-effect (writing
        to `out_dir`) happens inside `run()` as well.
        """
        cfg = self.config
        date_iso = cfg.target_date.isoformat()
        events_path = cfg.cache_dir / "events" / f"{date_iso}.json"
        sessions_path = cfg.cache_dir / "sessions" / f"{date_iso}.json"

        # --- Step 1: capture (or reuse cached events) ----------------------

        if cfg.reuse_cache or cfg.only_stage is not None:
            if not events_path.exists():
                raise FileNotFoundError(
                    f"--reuse-cache / --stage requires {events_path} to exist; "
                    f"run without those flags first."
                )
            events = _load_events(events_path)
            log.info("Loaded %d cached events from %s", len(events), events_path)
        else:
            events = self._capture_all()
            _save_events(events, events_path)
            log.info("Captured and cached %d events to %s", len(events), events_path)

        # --- Step 2: --no-llm short-circuit --------------------------------

        if cfg.skip_llm:
            debug_path = cfg.out_dir / f"{date_iso}.debug.md"
            cfg.out_dir.mkdir(parents=True, exist_ok=True)
            debug_path.write_text(_format_debug_dump(events), encoding="utf-8")
            log.info("Wrote --no-llm debug dump to %s", debug_path)
            return []

        # --- Step 3: cluster (or reuse cached sessions) --------------------

        if cfg.only_stage == "narrate" and sessions_path.exists():
            sessions = _load_sessions(sessions_path, events_by_ref=_index(events))
            log.info("Loaded %d cached sessions from %s", len(sessions), sessions_path)
        else:
            sessions = cluster_events(
                events,
                llm=cfg.llm,
                model=cfg.cluster_model,
                matter_resolver=cfg.mapper.resolve,
            )
            _save_sessions(sessions, sessions_path)
            log.info("Clustered into %d sessions; cached to %s", len(sessions), sessions_path)

        if cfg.only_stage == "cluster":
            log.info("Stopping after cluster stage as requested.")
            return []

        # --- Step 4: rollup (calls narrate per bucket) + render -----------

        entries = rollup_day(
            target_date=cfg.target_date,
            sessions=sessions,
            mapper=cfg.mapper,
            llm=cfg.llm,
            narrate_model=cfg.narrate_model,
            policy=cfg.policy,
        )
        out_path = cfg.renderer.render(
            target_date=cfg.target_date,
            entries=entries,
            out_dir=cfg.out_dir,
        )
        log.info("Wrote %d entries to %s", len(entries), out_path)
        return entries

    # --- internals ---------------------------------------------------------

    def _capture_all(self) -> list[Event]:
        events: list[Event] = []
        for adapter in self.config.adapters:
            try:
                adapter_events = adapter.capture(self.config.target_date)
                log.info("Adapter %s produced %d events", adapter.name, len(adapter_events))
                events.extend(adapter_events)
            except Exception:
                log.exception("Adapter %s failed; continuing without it.", adapter.name)
        events.sort(key=lambda e: e.timestamp)
        return events


# --- serialization helpers --------------------------------------------------


def _save_events(events: list[Event], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [_event_to_dict(e) for e in events]
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def _load_events(path: Path) -> list[Event]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [_event_from_dict(d) for d in data]


def _event_to_dict(e: Event) -> dict:
    return {
        "timestamp": e.timestamp.isoformat(),
        "duration_seconds": e.duration.total_seconds() if e.duration else None,
        "source": e.source,
        "project_hint": e.project_hint,
        "artifact_ref": e.artifact_ref,
        "content_excerpt": e.content_excerpt,
        "raw": e.raw,
    }


def _event_from_dict(d: dict) -> Event:
    dur = d.get("duration_seconds")
    return Event(
        timestamp=datetime.fromisoformat(d["timestamp"]),
        duration=timedelta(seconds=dur) if dur is not None else None,
        source=d["source"],
        project_hint=d.get("project_hint"),
        artifact_ref=d["artifact_ref"],
        content_excerpt=d.get("content_excerpt", ""),
        raw=d.get("raw") or {},
    )


def _save_sessions(sessions: list[Session], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [
        {
            "start": s.start.isoformat(),
            "end": s.end.isoformat(),
            "matter_id": s.matter_id,
            "category": s.category,
            "summary_hint": s.summary_hint,
            "evidence": [ev.artifact_ref for ev in s.events],
        }
        for s in sessions
    ]
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _load_sessions(path: Path, *, events_by_ref: dict[str, Event]) -> list[Session]:
    data = json.loads(path.read_text(encoding="utf-8"))
    out: list[Session] = []
    for d in data:
        out.append(
            Session(
                start=datetime.fromisoformat(d["start"]),
                end=datetime.fromisoformat(d["end"]),
                matter_id=d.get("matter_id"),
                events=[events_by_ref[r] for r in d.get("evidence", []) if r in events_by_ref],
                category=d.get("category", "building"),
                summary_hint=d.get("summary_hint", ""),
            )
        )
    return out


def _index(events: list[Event]) -> dict[str, Event]:
    return {e.artifact_ref: e for e in events}


def _format_debug_dump(events: list[Event]) -> str:
    """Plain human-readable dump of raw events. Used by --no-llm."""
    lines = [f"# Debug event dump ({len(events)} events)", ""]
    for e in events:
        ts = e.timestamp.strftime("%Y-%m-%d %H:%M:%S")
        lines.append(f"## [{ts}] {e.source} — {e.project_hint or '(no hint)'}")
        lines.append(f"- artifact: `{e.artifact_ref}`")
        if e.duration:
            lines.append(f"- duration: {int(e.duration.total_seconds() / 60)} min")
        lines.append("")
        lines.append("```")
        lines.append(e.content_excerpt)
        lines.append("```")
        lines.append("")
    return "\n".join(lines)
