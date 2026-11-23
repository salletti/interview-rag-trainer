from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class RetryIngestion:
    """Rouvrir l'item ``FAILED`` ``item_id`` (knowledge-base.md §5.2, §5.3)."""

    item_id: UUID
