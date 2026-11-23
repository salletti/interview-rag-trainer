"""Port de parcours de l'index vectoriel pour la réconciliation (S8 ;
knowledge-base.md §9 : « points en trop », manifests revérifiés ; ADR 0010
point 9).

Distinct de ``VectorIndex`` (écriture, vérification, visibilité), qui n'est
pas élargi : les doublures de ``VectorIndex`` des tests restent valides,
seul l'adapter réel porte le parcours. ``ReconcilableIndex`` réunit les deux
pour la réconciliation.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Collection, Mapping
from typing import Protocol
from uuid import UUID

from knowledge_base.domain.providers.vector_index import VectorIndex
from knowledge_base.domain.value_objects.indexed_point import PointSelector
from knowledge_base.domain.value_objects.point_record import PointRecord


class PointInventory(Protocol):
    def scan_points(
        self, index_id: UUID, *, batch_size: int = 256
    ) -> AsyncIterator[PointRecord]:
        """Tous les points de l'index, sans vecteur, chacun exactement une
        fois, lus par pages de ``batch_size``."""
        ...

    async def points_of(
        self, index_id: UUID, selector: PointSelector
    ) -> Mapping[UUID, str]:
        """``chunk_hash`` de chaque point de la sélection (une version, en
        général), par identifiant de point."""
        ...

    async def delete_point_ids(
        self, index_id: UUID, point_ids: Collection[UUID]
    ) -> None:
        """Supprime ces points ; un identifiant absent est sans effet."""
        ...


class ReconcilableIndex(VectorIndex, PointInventory, Protocol):
    """L'index vectoriel que la réconciliation lit, corrige et parcourt."""
