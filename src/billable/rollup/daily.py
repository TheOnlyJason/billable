"""Per-day rollup logic.

For each (date, matter) pair:
    1. Collect all Sessions in that bucket.
    2. Sum durations -> apply the rounding policy from `config/billing.yaml`.
    3. Drop entries below `minimum_entry_hours`.
    4. Ask the narrate stage to produce ONE description per bucket.
    5. Emit one `Entry` per surviving bucket.

Sessions with `matter_id is None` are emitted under a synthetic
`unclassified` matter so the user can review and re-tag them.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Literal

import yaml

from billable.core.events import Entry, Session
from billable.core.mapper import ProjectMapper
from billable.llm.client import LLMClient
from billable.llm.narrate import narrate_bucket

Rounding = Literal["up", "nearest", "down"]
_VALID_ROUNDINGS: set[Rounding] = {"up", "nearest", "down"}

UNCLASSIFIED = "unclassified"


@dataclass(frozen=True)
class BillingPolicy:
    increment_hours: Decimal
    rounding: Rounding
    minimum_entry_hours: Decimal
    daily_cutoff: str  # "HH:MM"

    @classmethod
    def from_yaml(cls, path: Path) -> BillingPolicy:
        if not path.exists():
            raise FileNotFoundError(f"Billing config not found at {path}")
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        rounding = data.get("rounding", "up")
        if rounding not in _VALID_ROUNDINGS:
            raise ValueError(
                f"{path}: 'rounding' must be one of {sorted(_VALID_ROUNDINGS)}, got {rounding!r}"
            )

        increment = Decimal(str(data.get("increment_hours", "0.25")))
        if increment <= 0:
            raise ValueError(f"{path}: 'increment_hours' must be positive, got {increment}")

        minimum = Decimal(str(data.get("minimum_entry_hours", "0.25")))
        if minimum < 0:
            raise ValueError(f"{path}: 'minimum_entry_hours' must be >= 0, got {minimum}")

        cutoff = str(data.get("daily_cutoff", "23:59"))
        # Light validation: HH:MM
        try:
            hh, mm = cutoff.split(":")
            int(hh)
            int(mm)
        except ValueError as e:
            raise ValueError(f"{path}: 'daily_cutoff' must be 'HH:MM', got {cutoff!r}") from e

        return cls(
            increment_hours=increment,
            rounding=rounding,
            minimum_entry_hours=minimum,
            daily_cutoff=cutoff,
        )


def round_to_increment(hours: Decimal, policy: BillingPolicy) -> Decimal:
    """Round `hours` to the configured billing increment using the configured policy.

    Returns a Decimal quantized to the increment (e.g. for 0.25 increment,
    output looks like Decimal('1.25')).
    """
    if hours <= 0:
        return Decimal("0").quantize(policy.increment_hours)

    increment = policy.increment_hours
    units = hours / increment

    if policy.rounding == "up":
        rounded_units = units.to_integral_value(rounding=ROUND_CEILING)
    elif policy.rounding == "down":
        rounded_units = units.to_integral_value(rounding=ROUND_DOWN)
    else:  # nearest
        rounded_units = units.to_integral_value(rounding=ROUND_HALF_UP)

    return (rounded_units * increment).quantize(increment)


def _sum_session_hours(sessions: list[Session]) -> Decimal:
    total = timedelta()
    for s in sessions:
        total += s.duration
    seconds = Decimal(str(total.total_seconds()))
    return seconds / Decimal("3600")


def rollup_day(
    *,
    target_date: date,
    sessions: list[Session],
    mapper: ProjectMapper,
    llm: LLMClient,
    narrate_model: str,
    policy: BillingPolicy,
) -> list[Entry]:
    """Collapse sessions for one day into Entries (one per matter).

    Sessions with `matter_id is None` are bucketed into a synthetic
    `unclassified` matter so the user can review and re-tag them rather
    than silently losing the time.
    """
    # Bucket by matter_id (None → "unclassified").
    buckets: dict[str, list[Session]] = defaultdict(list)
    for s in sessions:
        buckets[s.matter_id or UNCLASSIFIED].append(s)

    entries: list[Entry] = []
    for matter_id, bucket_sessions in buckets.items():
        raw_hours = _sum_session_hours(bucket_sessions)
        rounded = round_to_increment(raw_hours, policy)
        if rounded < policy.minimum_entry_hours:
            continue

        display_name = mapper.display_name(matter_id)
        description = narrate_bucket(
            date_iso=target_date.isoformat(),
            matter_id=matter_id,
            matter_display_name=display_name,
            sessions=bucket_sessions,
            llm=llm,
            model=narrate_model,
        )
        sources = [ev.artifact_ref for s in bucket_sessions for ev in s.events]
        entries.append(
            Entry(
                date=target_date,
                matter_id=matter_id,
                description=description,
                hours=rounded,
                sources=sources,
            )
        )

    # Stable order: real matters first (alphabetical), unclassified last.
    entries.sort(key=lambda e: (e.matter_id == UNCLASSIFIED, e.matter_id))
    return entries
