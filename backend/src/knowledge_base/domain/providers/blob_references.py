"""Port de lecture des références d'un original (ADR 0012 point 6) : un blob
n'est supprimé que si aucun document non purgé ne le porte."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from knowledge_base.domain.value_objects.content_hash import ContentHash


class BlobReferences(Protocol):
    async def holders(self, content_hash: ContentHash) -> frozenset[UUID]:
        """Documents dont au moins une version, dans quelque état que ce soit,
        porte cette empreinte, et dont la purge n'est pas enregistrée
        (``purged_at`` nul) : un tombstone purgé ne retient plus son blob.
        Lecture hors de toute transaction métier (état commité)."""
        ...
