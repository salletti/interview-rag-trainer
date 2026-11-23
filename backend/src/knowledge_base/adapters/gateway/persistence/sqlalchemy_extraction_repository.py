"""Écriture du lot d'unités et de chunks d'une version (knowledge-base.md §3.4,
I7, I13) : deux ``INSERT`` multi-lignes dans la session de la transaction de
finalisation ; ne valide ni n'annule jamais la transaction.

Identifiants déterministes : un second lot pour la même version et la même
politique viole les clés primaires, donc annule la transaction (I7 : un lot
n'est jamais réécrit).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_base.domain.value_objects.citation import Citation
from knowledge_base.domain.value_objects.document_chunk import DocumentChunk
from knowledge_base.domain.value_objects.knowledge_unit import KnowledgeUnit
from knowledge_base.domain.value_objects.structured_document import render_blocks
from knowledge_base.frameworks.sqlalchemy.tables import (
    document_chunks,
    knowledge_units,
)


class SqlAlchemyExtractionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_batch(
        self,
        version_id: UUID,
        policy_version: str,
        units: Sequence[KnowledgeUnit],
        chunks: Sequence[DocumentChunk],
    ) -> None:
        if units:
            await self._session.execute(
                insert(knowledge_units),
                [_unit_values(version_id, policy_version, u) for u in units],
            )
        if chunks:
            await self._session.execute(
                insert(document_chunks),
                [_chunk_values(version_id, policy_version, c) for c in chunks],
            )


def _unit_values(
    version_id: UUID, policy_version: str, unit: KnowledgeUnit
) -> dict[str, Any]:
    return {
        "id": unit.unit_id,
        "version_id": version_id,
        "chunking_policy_version": policy_version,
        "ordinal": unit.ordinal,
        "unit_type": unit.unit_type.value,
        "question_text": unit.question_text,
        "section_path": list(unit.section_path.parts),
        "page_start": unit.page_span.start,
        "page_end": unit.page_span.end,
        "token_count": unit.token_count,
        "text": render_blocks(unit.blocks),
    }


def _chunk_values(
    version_id: UUID, policy_version: str, chunk: DocumentChunk
) -> dict[str, Any]:
    return {
        "id": chunk.chunk_id,
        "unit_id": chunk.unit_id,
        "version_id": version_id,
        "chunking_policy_version": policy_version,
        "ordinal_in_unit": chunk.ordinal_in_unit,
        "contextual_text": chunk.contextual_text,
        "chunk_hash": chunk.chunk_hash,
        "token_count": chunk.token_count,
        "citation": citation_document(chunk.citation),
    }


def citation_document(citation: Citation) -> dict[str, Any]:
    """Forme JSON de la citation : une clé par champ de ``Citation``."""
    return {
        "document_id": str(citation.document_id),
        "document_title": citation.document_title,
        "version_id": str(citation.version_id),
        "version_number": citation.version_number,
        "section_path": list(citation.section_path.parts),
        "page_start": citation.page_start,
        "page_end": citation.page_end,
        "source_filename": citation.source_filename,
    }
