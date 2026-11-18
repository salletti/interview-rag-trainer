"""Refus de ``PublishDocumentVersion`` qui ne relèvent pas de l'agrégat
``KnowledgeDocument`` (knowledge-base.md §8, I9). Les refus de l'agrégat (I5,
version non ``EXTRACTED``, document retiré) remontent en
``KnowledgeDocumentError``."""

from __future__ import annotations

from uuid import UUID


class PublicationRefusedError(Exception):
    def __init__(self, version_id: UUID, message: str) -> None:
        super().__init__(f"publication de la version {version_id} refusée : {message}")
        self.version_id = version_id


class NoActiveIndexError(PublicationRefusedError):
    def __init__(self, version_id: UUID) -> None:
        super().__init__(version_id, "aucun index actif dans le corpus")


class IndexationNotCompletedError(PublicationRefusedError):
    """I9 : l'indexation de la version dans l'index actif est absente ou n'est
    pas ``COMPLETED``."""

    def __init__(self, version_id: UUID, index_id: UUID) -> None:
        super().__init__(
            version_id, f"indexation non COMPLETED dans l'index actif {index_id}"
        )
        self.index_id = index_id
