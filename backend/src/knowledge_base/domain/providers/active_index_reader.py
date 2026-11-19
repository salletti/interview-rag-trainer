"""Port de lecture de l'index actif du corpus pour la recherche
(knowledge-base.md §12 étape 1).

Lecture simple, sans verrou : la recherche n'écrit rien et n'a pas à attendre
une activation. Elle lit l'index actif au moment de la requête.
"""

from __future__ import annotations

from typing import Protocol

from knowledge_base.domain.entities.knowledge_corpus import SearchIndex


class ActiveIndexReader(Protocol):
    async def get_active_index(self) -> SearchIndex | None:
        """``None`` si le corpus n'existe pas ou n'a aucun index actif."""
        ...
