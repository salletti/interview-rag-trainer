"""Port du moteur vectoriel (knowledge-base.md §9, §11 ; ADR 0010).

``index_id`` désigne la collection de l'index ; son nom est calculé par
l'adapter. Toutes les écritures sont idempotentes (§7.3) : identifiants
déterministes, upserts qui écrasent, suppressions par filtre.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from knowledge_base.domain.value_objects.embedding_profile import EmbeddingProfile
from knowledge_base.domain.value_objects.indexed_point import (
    IndexedPoint,
    PointSelector,
)


class VectorIndex(Protocol):
    async def ensure_collection(
        self, index_id: UUID, profile: EmbeddingProfile
    ) -> None:
        """Crée la collection (dimension et distance du profil) et les index
        de payload des filtres ; sans effet si elle existe déjà."""
        ...

    async def check_collection(self, index_id: UUID, profile: EmbeddingProfile) -> bool:
        """Vrai si la collection existe avec la dimension et la distance du
        profil."""
        ...

    async def upsert(
        self, index_id: UUID, points: Sequence[IndexedPoint], *, wait: bool = True
    ) -> None:
        """``wait`` : rend la main une fois l'écriture appliquée et lisible ;
        lève si elle n'a pas abouti."""
        ...

    async def retrieve(
        self, index_id: UUID, point_ids: Sequence[UUID]
    ) -> Sequence[tuple[UUID, str]]:
        """Couples ``(point_id, chunk_hash)`` des points qui existent parmi
        ``point_ids``, sans vecteur ; les absents sont omis."""
        ...

    async def set_visibility(
        self, index_id: UUID, *, document_id: UUID, version_id: UUID, visible: bool
    ) -> None: ...

    async def delete_points(self, index_id: UUID, selector: PointSelector) -> None: ...
