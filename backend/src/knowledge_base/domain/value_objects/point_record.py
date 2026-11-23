"""Point lu dans l'index vectoriel pour la réconciliation (S8 ; knowledge-base.md
§9) : propriétaire, empreinte et visibilité, sans vecteur. DTO du port
``PointInventory``, comme ``IndexedPoint`` l'est de ``VectorIndex``."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class PointRecord:
    point_id: UUID
    document_id: UUID
    version_id: UUID
    chunk_hash: str
    visible: bool
