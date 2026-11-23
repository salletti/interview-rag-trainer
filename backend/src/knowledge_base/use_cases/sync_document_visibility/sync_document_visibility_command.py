from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class SyncDocumentVisibility:
    """Payload de l'item ``sync_visibility`` ; ``visibility_revision`` n'est
    qu'une identité (dédoublonnage), la convergence vise l'état courant."""

    document_id: UUID
    visibility_revision: int
