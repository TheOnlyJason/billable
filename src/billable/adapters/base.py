"""Capture adapter interface.

Every adapter must implement this protocol. Adapters are stateless from the
pipeline's perspective: `capture(target_date)` is called once per run and
returns all events for that day.

Adapters are responsible for:
    - Reading from their source (SQLite, REST API, filesystem, etc.).
    - Emitting Events that conform to `core.events.Event`.
    - Calling `core.sanitize.sanitize` on every `content_excerpt` before
      returning. (Centralized policy, decentralized enforcement.)
    - Truncating overly long excerpts so we don't pay for token bloat.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date

from billable.core.events import Event


class CaptureAdapter(ABC):
    """Abstract base for all capture adapters."""

    name: str

    @abstractmethod
    def capture(self, target_date: date) -> list[Event]:
        """Return all events from this source for the local date `target_date`.

        Implementations should:
            - Be idempotent: calling twice for the same date returns equivalent
              events (modulo source mutations).
            - Honor `config/billing.yaml`'s `daily_cutoff`.
            - Sanitize every Event's `content_excerpt`.
        """
        ...
