"""Refus des cas d'usage de Knowledge Base qui ne relèvent pas d'un invariant
de l'agrégat (ceux-ci lèvent une ``KnowledgeDocumentError`` du domaine)."""

from __future__ import annotations

from uuid import UUID


class UnsupportedDocumentTypeError(Exception):
    """Type de média ou extension hors de la liste blanche (ADR 0012)."""

    def __init__(self, filename: str, media_type: str) -> None:
        super().__init__(
            f"type non pris en charge : fichier {filename!r}, type {media_type!r}"
        )
        self.filename = filename
        self.media_type = media_type


class InvalidFilenameError(Exception):
    """Nom de fichier inutilisable une fois réduit à son nom final."""

    def __init__(self, filename: str, reason: str) -> None:
        super().__init__(f"nom de fichier refusé ({reason}) : {filename[:300]!r}")
        self.filename = filename
        self.reason = reason


class DocumentNotFoundError(Exception):
    def __init__(self, document_id: UUID) -> None:
        super().__init__(f"document {document_id} inconnu")
        self.document_id = document_id


class DuplicateContentOtherDocumentError(Exception):
    """Décision O2 : ce contenu appartient déjà à un autre document ;
    ``document_id`` et ``version_id`` désignent la version existante."""

    def __init__(self, document_id: UUID, version_id: UUID) -> None:
        super().__init__(
            f"contenu déjà importé par le document {document_id} (version {version_id})"
        )
        self.document_id = document_id
        self.version_id = version_id


class IdempotencyKeyConflictError(Exception):
    """Clé déjà utilisée pour une requête différente."""

    def __init__(self, key: str) -> None:
        super().__init__(
            f"clé d'idempotence {key!r} déjà utilisée pour une autre requête"
        )
        self.key = key
