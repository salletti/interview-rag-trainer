from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class ActivateSearchIndex:
    """Index ``BUILDING`` à rendre ``ACTIVE`` (knowledge-base.md §13)."""

    index_id: UUID
