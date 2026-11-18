"""Point à écrire dans l'index vectoriel et sélection de points à supprimer
(knowledge-base.md §9 ; ADR 0010 §2, §7).

Les champs de ``IndexedPoint`` (hors ``point_id`` et ``vector``) sont ceux du
payload : identité et empreinte du chunk pour la vérification du manifest,
``visible`` et champs de filtrage pour la recherche.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class IndexedPoint:
    point_id: UUID
    vector: tuple[float, ...]
    chunk_id: UUID
    unit_id: UUID
    version_id: UUID
    document_id: UUID
    chunk_hash: str
    index_id: UUID
    # Toujours faux à l'écriture : seul ``sync_visibility`` rend un point
    # visible, après la publication SQL (§11).
    visible: bool
    topics: tuple[str, ...]
    kind: str
    language: str


@dataclass(frozen=True)
class PointSelector:
    """Les points d'une version, ou de tout le document si ``version_id`` est
    ``None`` : un filtre déterministe, répétable sans effet de bord (§7.3)."""

    document_id: UUID
    version_id: UUID | None = None
