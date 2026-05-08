"""Renderer interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from pathlib import Path

from billable.core.events import Entry


class Renderer(ABC):
    """Abstract base for all output renderers."""

    name: str
    extension: str  # e.g. "md", "docx"

    @abstractmethod
    def render(
        self,
        *,
        target_date: date,
        entries: list[Entry],
        out_dir: Path,
    ) -> Path:
        """Write the rendered document and return the path written.

        Implementations are responsible for filename convention. The default
        convention is `out_dir / f"{target_date.isoformat()}.{extension}"`.
        """
        ...
