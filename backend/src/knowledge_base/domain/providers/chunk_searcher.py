"""Port de recherche vectorielle groupée par unité (knowledge-base.md §12
étape 3 ; ADR 0010 §5).

Le filtre ``visible=true`` est appliqué par l'implémentation, toujours : il
n'est pas un paramètre, pour qu'aucun appelant ne puisse l'oublier. Il ne
décide pas de la visibilité (le contrôle SQL final s'en charge), il préserve
le Recall en écartant les points obsolètes avant le top-k.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from typing import Protocol
from uuid import UUID

from knowledge_base.domain.value_objects.chunk_hit import ChunkHit
from knowledge_base.domain.value_objects.search_filters import SearchFilters


class ChunkSearcher(Protocol):
    async def search(
        self,
        index_id: UUID,
        vector: Sequence[float],
        *,
        unit_limit: int,
        filters: SearchFilters,
        excluded_unit_ids: Collection[UUID],
    ) -> Sequence[ChunkHit]:
        """Chunks des ``unit_limit`` meilleures unités distinctes (les
        meilleures d'abord), parmi les points visibles conformes aux filtres,
        hors ``excluded_unit_ids`` ; plusieurs chunks d'une même unité
        peuvent être rendus."""
        ...
