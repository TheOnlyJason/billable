"""Tests for BillingPolicy + rounding + rollup_day.

Rollup tests use a fake LLM so we can verify aggregation/rounding logic
without network calls.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from billable.core.events import Event, Session
from billable.core.mapper import ProjectMapper, ProjectRule
from billable.llm.client import LLMClient
from billable.rollup.daily import (
    UNCLASSIFIED,
    BillingPolicy,
    round_to_increment,
    rollup_day,
)


# --- helpers ----------------------------------------------------------------


class FakeLLM(LLMClient):
    """Returns canned narrative descriptions; records every call."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def complete_json(
        self,
        *,
        system: str,
        user: str,
        model: str,
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        self.calls.append({"system": system, "user": user, "model": model})
        return {"description": f"Generated narrative for call #{len(self.calls)}."}


def _ev(ref: str = "ref") -> Event:
    return Event(
        timestamp=datetime(2026, 5, 7, 10, 0),
        duration=timedelta(minutes=10),
        source="cursor",
        project_hint="x",
        artifact_ref=ref,
        content_excerpt="content",
    )


def _session(start_h: int, end_h: int, matter_id: str | None, refs: list[str]) -> Session:
    return Session(
        start=datetime(2026, 5, 7, start_h, 0),
        end=datetime(2026, 5, 7, end_h, 0),
        matter_id=matter_id,
        events=[_ev(r) for r in refs],
        category="building",
        summary_hint="hint",
    )


def _policy(
    *,
    increment: str = "0.25",
    rounding: str = "up",
    minimum: str = "0.25",
) -> BillingPolicy:
    return BillingPolicy(
        increment_hours=Decimal(increment),
        rounding=rounding,  # type: ignore[arg-type]
        minimum_entry_hours=Decimal(minimum),
        daily_cutoff="23:59",
    )


# --- BillingPolicy.from_yaml -----------------------------------------------


def test_billing_policy_from_yaml_defaults(tmp_path: Path) -> None:
    p = tmp_path / "billing.yaml"
    p.write_text("increment_hours: 0.25\nrounding: up\nminimum_entry_hours: 0.25\n")
    pol = BillingPolicy.from_yaml(p)
    assert pol.increment_hours == Decimal("0.25")
    assert pol.rounding == "up"
    assert pol.minimum_entry_hours == Decimal("0.25")
    assert pol.daily_cutoff == "23:59"


def test_billing_policy_invalid_rounding(tmp_path: Path) -> None:
    p = tmp_path / "billing.yaml"
    p.write_text("rounding: sideways\n")
    with pytest.raises(ValueError, match="rounding"):
        BillingPolicy.from_yaml(p)


def test_billing_policy_invalid_increment(tmp_path: Path) -> None:
    p = tmp_path / "billing.yaml"
    p.write_text("increment_hours: 0\n")
    with pytest.raises(ValueError, match="increment_hours"):
        BillingPolicy.from_yaml(p)


def test_billing_policy_invalid_cutoff(tmp_path: Path) -> None:
    p = tmp_path / "billing.yaml"
    p.write_text("daily_cutoff: 'midnight'\n")
    with pytest.raises(ValueError, match="daily_cutoff"):
        BillingPolicy.from_yaml(p)


# --- round_to_increment -----------------------------------------------------


@pytest.mark.parametrize(
    ("hours", "expected"),
    [
        ("0.0",  "0.00"),
        ("0.01", "0.25"),
        ("0.25", "0.25"),
        ("0.26", "0.50"),
        ("1.00", "1.00"),
        ("1.24", "1.25"),
        ("1.25", "1.25"),
        ("1.26", "1.50"),
        ("2.74", "2.75"),
    ],
)
def test_round_up_at_quarter_increments(hours: str, expected: str) -> None:
    out = round_to_increment(Decimal(hours), _policy(rounding="up"))
    assert out == Decimal(expected)


@pytest.mark.parametrize(
    ("hours", "expected"),
    [
        ("0.10", "0.10"),
        ("0.11", "0.20"),
        ("0.99", "1.00"),
        ("1.00", "1.00"),
        ("1.01", "1.10"),
    ],
)
def test_round_up_at_tenth_increments(hours: str, expected: str) -> None:
    out = round_to_increment(Decimal(hours), _policy(increment="0.1", rounding="up"))
    assert out == Decimal(expected)


def test_round_nearest() -> None:
    pol = _policy(rounding="nearest")
    assert round_to_increment(Decimal("0.124"), pol) == Decimal("0.00")
    assert round_to_increment(Decimal("0.125"), pol) == Decimal("0.25")  # half rounds up
    assert round_to_increment(Decimal("0.376"), pol) == Decimal("0.50")


def test_round_down() -> None:
    pol = _policy(rounding="down")
    assert round_to_increment(Decimal("0.74"), pol) == Decimal("0.50")
    assert round_to_increment(Decimal("1.99"), pol) == Decimal("1.75")


def test_round_negative_or_zero_yields_zero() -> None:
    pol = _policy()
    assert round_to_increment(Decimal("0"), pol) == Decimal("0.00")
    assert round_to_increment(Decimal("-1"), pol) == Decimal("0.00")


# --- rollup_day -------------------------------------------------------------


def _mapper() -> ProjectMapper:
    return ProjectMapper(
        rules=(
            ProjectRule(matter_id="acme", display_name="Acme Corp"),
            ProjectRule(matter_id="internal", display_name="Internal R&D"),
        )
    )


def test_rollup_groups_sessions_by_matter_and_rounds_up() -> None:
    sessions = [
        _session(9, 10, "acme", ["a1"]),               # 1.0h
        _session(10, 11, "acme", ["a2"]),              # 1.0h → acme total 2.0h
        _session(13, 14, "internal", ["i1"]),          # 1.0h
    ]
    llm = FakeLLM()
    entries = rollup_day(
        target_date=date(2026, 5, 7),
        sessions=sessions,
        mapper=_mapper(),
        llm=llm,
        narrate_model="x",
        policy=_policy(),
    )
    assert len(entries) == 2
    # Sorted: acme alphabetically before internal, both before unclassified.
    assert [e.matter_id for e in entries] == ["acme", "internal"]
    assert entries[0].hours == Decimal("2.00")
    assert entries[1].hours == Decimal("1.00")
    assert entries[0].sources == ["a1", "a2"]
    assert entries[1].sources == ["i1"]
    # Narrate stage was called once per matter.
    assert len(llm.calls) == 2


def test_rollup_unclassified_bucketed_separately_and_emitted_last() -> None:
    sessions = [
        _session(9, 10, "acme", ["a1"]),
        _session(10, 11, None, ["u1"]),
    ]
    entries = rollup_day(
        target_date=date(2026, 5, 7),
        sessions=sessions,
        mapper=_mapper(),
        llm=FakeLLM(),
        narrate_model="x",
        policy=_policy(),
    )
    assert [e.matter_id for e in entries] == ["acme", UNCLASSIFIED]


def test_rollup_drops_entries_below_minimum() -> None:
    # A 5-minute session rounds up to 0.25h with default policy, which equals
    # the minimum — so it survives. With a higher minimum it should drop.
    short = Session(
        start=datetime(2026, 5, 7, 9, 0),
        end=datetime(2026, 5, 7, 9, 5),
        matter_id="acme",
        events=[_ev("e1")],
        category="building",
    )
    entries = rollup_day(
        target_date=date(2026, 5, 7),
        sessions=[short],
        mapper=_mapper(),
        llm=FakeLLM(),
        narrate_model="x",
        policy=_policy(minimum="0.50"),
    )
    assert entries == []


def test_rollup_empty_sessions_returns_empty() -> None:
    entries = rollup_day(
        target_date=date(2026, 5, 7),
        sessions=[],
        mapper=_mapper(),
        llm=FakeLLM(),
        narrate_model="x",
        policy=_policy(),
    )
    assert entries == []
