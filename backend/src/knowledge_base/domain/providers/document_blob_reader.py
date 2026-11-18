"""Port de lecture des originaux déposés à l'import (ADR 0012)."""

from __future__ import annotations

from typing import Protocol

from knowledge_base.domain.value_objects.content_hash import ContentHash


class BlobMissingError(Exception):
    """Aucun original sous cette empreinte alors qu'une version la référence :
    incohérence durable du stockage, qu'une nouvelle tentative ne corrige pas."""

    def __init__(self, content_hash: ContentHash) -> None:
        super().__init__(f"original {content_hash.value} introuvable")
        self.content_hash = content_hash


class DocumentBlobReader(Protocol):
    async def read(self, content_hash: ContentHash) -> bytes:
        """Contenu complet ; lève ``BlobMissingError`` s'il est absent."""
        ...
