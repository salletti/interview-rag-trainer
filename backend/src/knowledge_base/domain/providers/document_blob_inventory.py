"""Port d'inventaire des originaux stockés (ADR 0012 point 6) : ce que le
nettoyage des orphelins examine."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from knowledge_base.domain.value_objects.content_hash import ContentHash


class StoredBlobEntry(Protocol):
    """Un original du volume ; ``modified_at`` (aware) date son dépôt et sert
    au délai de grâce."""

    @property
    def content_hash(self) -> ContentHash: ...

    @property
    def modified_at(self) -> datetime: ...


class DocumentBlobInventory(Protocol):
    async def list_blobs(self) -> Sequence[StoredBlobEntry]:
        """Originaux adressés par contenu ; les fichiers temporaires d'un
        dépôt en cours n'en font pas partie. Volume vide : séquence vide."""
        ...
