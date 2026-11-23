"""Port du contrôle SQL final de la recherche (knowledge-base.md §11, §12
étape 4 ; ADR 0010 §4, §6 ; I12).

PostgreSQL arbitre la visibilité : une unité n'est rendue que si sa version
est ``PUBLISHED`` et son document ``ACTIVE``. Qdrant n'en porte qu'une copie
(le drapeau ``visible``), qui peut être en retard.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol
from uuid import UUID

from knowledge_base.domain.value_objects.published_unit import PublishedUnit


class PublishedUnitReader(Protocol):
    async def read_published(
        self, unit_ids: Sequence[UUID]
    ) -> Mapping[UUID, PublishedUnit]:
        """Unités visibles parmi ``unit_ids``, avec leurs chunks et leur
        citation ; une unité qui ne l'est pas (ou plus) est absente.

        Une seule lecture cohérente (un instantané) pour toutes les unités
        demandées : deux unités de versions différentes d'un même document ne
        peuvent pas y être visibles ensemble (I4)."""
        ...
