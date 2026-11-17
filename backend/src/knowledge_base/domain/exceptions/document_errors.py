"""Refus métier de l'agrégat ``KnowledgeDocument`` (knowledge-base.md §4.1, §5)."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from knowledge_base.domain.entities.knowledge_document import VersionStatus
    from knowledge_base.domain.value_objects.content_hash import ContentHash


class KnowledgeDocumentError(Exception):
    """Base : l'opération est refusée et l'agrégat est inchangé."""

    def __init__(self, document_id: UUID, message: str) -> None:
        super().__init__(f"document {document_id} : {message}")
        self.document_id = document_id


class DuplicateContentError(KnowledgeDocumentError):
    """I3 : cette empreinte est déjà celle d'une version du document, quel que
    soit son état (``DISCARDED`` compris)."""

    def __init__(
        self, document_id: UUID, content_hash: ContentHash, existing_version_id: UUID
    ) -> None:
        super().__init__(
            document_id,
            f"contenu {content_hash.value} déjà importé "
            f"(version {existing_version_id})",
        )
        self.content_hash = content_hash
        self.existing_version_id = existing_version_id


class DocumentWithdrawnError(KnowledgeDocumentError):
    def __init__(self, document_id: UUID, operation: str) -> None:
        super().__init__(document_id, f"retiré, {operation} refusé")
        self.operation = operation


class VersionNotFoundError(KnowledgeDocumentError):
    def __init__(self, document_id: UUID, version_id: UUID) -> None:
        super().__init__(document_id, f"version {version_id} inconnue")
        self.version_id = version_id


class InvalidVersionTransitionError(KnowledgeDocumentError):
    """Transition absente du diagramme de ``DocumentVersion`` (§5.2)."""

    def __init__(
        self,
        document_id: UUID,
        version_id: UUID,
        current: VersionStatus,
        target: VersionStatus,
    ) -> None:
        super().__init__(
            document_id, f"version {version_id} : {current} -> {target} interdit"
        )
        self.version_id = version_id
        self.current = current
        self.target = target


class StalePublicationError(KnowledgeDocumentError):
    """I5 : seule une version plus récente que la publiée peut être publiée."""

    def __init__(
        self, document_id: UUID, version_number: int, published_number: int
    ) -> None:
        super().__init__(
            document_id,
            f"version n°{version_number} non postérieure à la version publiée "
            f"n°{published_number}",
        )
        self.version_number = version_number
        self.published_number = published_number
