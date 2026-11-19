from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class PurgeDocument:
    """Payload de l'item ``purge_document``."""

    document_id: UUID
