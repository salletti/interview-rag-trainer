from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class RechunkVersion:
    """Payload de l'item ``rechunk_version`` : produire le lot de chunks de la
    version pour la politique de l'index."""

    document_id: UUID
    version_id: UUID
    index_id: UUID
