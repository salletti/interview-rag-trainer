"""Port d'écriture des unités et chunks d'une version (knowledge-base.md §3.4,
I7) : un lot identifié par ``(version_id, policy_version)``, écrit dans la
transaction qui fait passer la version à ``EXTRACTED`` et jamais modifié."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from knowledge_base.domain.value_objects.document_chunk import DocumentChunk
from knowledge_base.domain.value_objects.knowledge_unit import KnowledgeUnit


class ExtractionRepository(Protocol):
    async def add_batch(
        self,
        version_id: UUID,
        policy_version: str,
        units: Sequence[KnowledgeUnit],
        chunks: Sequence[DocumentChunk],
    ) -> None: ...
