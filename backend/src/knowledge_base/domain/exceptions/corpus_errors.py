"""Refus de l'agrégat ``KnowledgeCorpus`` (knowledge-base.md §3.2, §5.4 ;
I10, I11, un seul index ``BUILDING``)."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from knowledge_base.domain.entities.knowledge_corpus import IndexStatus


class KnowledgeCorpusError(Exception):
    """Base : l'opération est refusée et le corpus est inchangé."""

    def __init__(self, index_id: UUID, message: str) -> None:
        super().__init__(f"index {index_id} : {message}")
        self.index_id = index_id


class UnknownIndexError(KnowledgeCorpusError):
    def __init__(self, index_id: UUID) -> None:
        super().__init__(index_id, "inconnu du corpus")


class DuplicateIndexError(KnowledgeCorpusError):
    def __init__(self, index_id: UUID) -> None:
        super().__init__(index_id, "déjà provisionné")


class IndexAlreadyBuildingError(KnowledgeCorpusError):
    """Un seul index ``BUILDING`` par corpus (une seule migration en cours) :
    ``index_id`` est celui de l'index déjà en construction."""

    def __init__(self, building_id: UUID) -> None:
        super().__init__(
            building_id,
            "déjà en construction : un seul index BUILDING par corpus, "
            "l'activer ou l'abandonner avant d'en provisionner un autre",
        )


class InvalidIndexTransitionError(KnowledgeCorpusError):
    """Transition absente du diagramme de ``SearchIndex`` (§5.4)."""

    def __init__(self, index_id: UUID, current: IndexStatus, operation: str) -> None:
        super().__init__(index_id, f"{operation} interdit depuis {current}")
        self.current = current
        self.operation = operation


class IncompleteCoverageError(KnowledgeCorpusError):
    """I11 : une version publiée d'un document actif n'est pas ``COMPLETED``
    dans l'index candidat."""

    def __init__(self, index_id: UUID) -> None:
        super().__init__(index_id, "couverture incomplète, activation refusée")
