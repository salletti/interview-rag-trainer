"""Lecture des chunks d'un lot d'extraction pour l'indexation
(knowledge-base.md §3.4) : une requête, dans l'ordre (unité, rang)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_base.domain.value_objects.citation import Citation
from knowledge_base.domain.value_objects.document_chunk import DocumentChunk
from knowledge_base.domain.value_objects.section_path import SectionPath
from knowledge_base.frameworks.sqlalchemy.tables import (
    document_chunks,
    knowledge_units,
)

_ch = document_chunks.c
_u = knowledge_units.c


class SqlAlchemyChunkReader:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_chunks(
        self, version_id: UUID, policy_version: str
    ) -> tuple[DocumentChunk, ...]:
        rows = await self._session.execute(
            select(
                _ch.id,
                _ch.unit_id,
                _ch.ordinal_in_unit,
                _ch.contextual_text,
                _ch.chunk_hash,
                _ch.token_count,
                _ch.citation,
            )
            .join(knowledge_units, _u.id == _ch.unit_id)
            .where(
                _ch.version_id == version_id,
                _ch.chunking_policy_version == policy_version,
            )
            .order_by(_u.ordinal, _ch.ordinal_in_unit)
        )
        return tuple(
            DocumentChunk(
                chunk_id=row.id,
                unit_id=row.unit_id,
                ordinal_in_unit=row.ordinal_in_unit,
                contextual_text=row.contextual_text,
                chunk_hash=row.chunk_hash,
                token_count=row.token_count,
                citation=_to_citation(row.citation),
            )
            for row in rows
        )


def _to_citation(document: dict[str, Any]) -> Citation:
    """Inverse de ``citation_document`` (``sqlalchemy_extraction_repository``)."""
    return Citation(
        document_id=UUID(document["document_id"]),
        document_title=document["document_title"],
        version_id=UUID(document["version_id"]),
        version_number=document["version_number"],
        section_path=SectionPath(tuple(document["section_path"])),
        page_start=document["page_start"],
        page_end=document["page_end"],
        source_filename=document["source_filename"],
    )
