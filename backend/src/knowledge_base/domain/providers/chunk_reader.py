"""Port de lecture des chunks d'un lot d'extraction (knowledge-base.md §3.4),
pour l'indexation."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from knowledge_base.domain.value_objects.document_chunk import DocumentChunk


class ChunkReader(Protocol):
    async def list_chunks(
        self, version_id: UUID, policy_version: str
    ) -> tuple[DocumentChunk, ...]:
        """Chunks du lot ``(version_id, policy_version)`` dans l'ordre (unité,
        rang) ; vide si le lot n'existe pas."""
        ...
