"""Port de persistance de l'agrégat ``KnowledgeCorpus`` (knowledge-base.md
§3.2, §13).

Les deux lectures verrouillent la ligne du corpus jusqu'à la fin de la
transaction : c'est la barrière blue/green (ADR 0011 §5).
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from knowledge_base.domain.entities.knowledge_corpus import KnowledgeCorpus


class CorpusAlreadyExistsError(Exception):
    """Une transaction concurrente a créé le corpus entre la lecture et
    l'écriture."""

    def __init__(self) -> None:
        super().__init__("corpus déjà créé par une transaction concurrente")


class CorpusModifiedConcurrentlyError(Exception):
    """Verrou optimiste du corpus perdu : il a changé depuis sa lecture."""

    def __init__(self, corpus_id: UUID) -> None:
        super().__init__(f"corpus {corpus_id} modifié par une autre transaction")
        self.corpus_id = corpus_id


class KnowledgeCorpusRepository(Protocol):
    async def get_shared(self) -> KnowledgeCorpus | None:
        """``SELECT … FOR SHARE`` : lecture de l'index actif ou des index
        vivants, compatible avec d'autres lectures partagées, bloquée par une
        activation en cours."""
        ...

    async def get_for_update(self) -> KnowledgeCorpus | None:
        """``SELECT … FOR UPDATE`` : modification du corpus (provisionnement,
        activation)."""
        ...

    async def add(self, corpus: KnowledgeCorpus) -> None:
        """Lève ``CorpusAlreadyExistsError`` si le corpus existe déjà."""
        ...

    async def save(self, corpus: KnowledgeCorpus) -> None:
        """Lève ``CorpusModifiedConcurrentlyError`` si le corpus a changé
        depuis sa lecture."""
        ...
