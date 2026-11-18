"""``KnowledgeSearch`` : recherche sémantique sourcée, en processus
(knowledge-base.md §14 ; ADR 0007 §2).

L'implémentation est injectée par ``runtime`` (fabrique
``knowledge_base.build_knowledge_search``). Erreurs rendues au consommateur :

- ``ValueError`` : requête invalide (texte vide, ``limit < 1``, type de
  document inconnu) ;
- ``KnowledgeSearchUnavailableError`` : la recherche ne peut pas répondre
  correctement (pas d'index actif, profil d'embeddings incompatible) ;
- les erreurs d'accès à PostgreSQL ou Qdrant sont propagées telles quelles :
  un résultat vide serait un faux « rien trouvé ».
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from knowledge_base.contracts.result import KnowledgeQuery, KnowledgeSearchResult


class UnavailabilityReason(StrEnum):
    NO_ACTIVE_SEARCH_INDEX = "NO_ACTIVE_SEARCH_INDEX"
    SEARCH_PROFILE_MISMATCH = "SEARCH_PROFILE_MISMATCH"


class KnowledgeSearchUnavailableError(Exception):
    def __init__(self, reason: UnavailabilityReason, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class KnowledgeSearch(Protocol):
    async def search(self, query: KnowledgeQuery) -> KnowledgeSearchResult: ...
