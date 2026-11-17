"""Port du stockage des originaux importés (ADR 0012).

Le dépôt a lieu **avant** la transaction d'import : un échec de celle-ci
laisse un blob orphelin, jamais une ligne sans blob.
"""

from __future__ import annotations

from collections.abc import AsyncIterable
from dataclasses import dataclass
from typing import Protocol

from knowledge_base.domain.value_objects.content_hash import ContentHash


@dataclass(frozen=True)
class StoredBlob:
    content_hash: ContentHash
    size_bytes: int


class BlobTooLargeError(Exception):
    """Le flux dépasse la limite : rien n'a été conservé."""

    def __init__(self, max_bytes: int) -> None:
        super().__init__(f"contenu refusé : plus de {max_bytes} octets")
        self.max_bytes = max_bytes


class DocumentBlobStore(Protocol):
    async def put(self, chunks: AsyncIterable[bytes], *, max_bytes: int) -> StoredBlob:
        """Consomme le flux une seule fois, calcule son SHA-256 et le conserve
        sous cette empreinte. Idempotent : un contenu déjà stocké n'est ni
        dupliqué ni altéré. Lève ``BlobTooLargeError`` dès que le flux dépasse
        ``max_bytes`` (sans le lire jusqu'au bout) ; en cas d'erreur, rien
        n'est conservé et l'erreur d'origine est propagée."""
        ...

    async def exists(self, content_hash: ContentHash) -> bool: ...
