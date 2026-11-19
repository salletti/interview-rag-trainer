from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class CompleteIndexation:
    """Payload de l'item ``index_version`` (knowledge-base.md §6)."""

    document_id: UUID
    version_id: UUID
    index_id: UUID
