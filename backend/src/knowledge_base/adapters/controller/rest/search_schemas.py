"""Schémas HTTP de la recherche (S5), distincts du contrat ``KnowledgeSearch``
et du cas d'usage : noms des champs du contrat (knowledge-base.md §14), UUID
en texte."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from knowledge_base.domain.value_objects.document_kind import DocumentKind
from knowledge_base.use_cases.search_knowledge.search_knowledge_command import (
    MAX_SEARCH_LIMIT,
    MAX_SEARCH_TEXT_LENGTH,
)


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Bornes de la query ``SearchKnowledge``, vérifiées ici pour un 422.
    # pattern : au moins un caractère non blanc (recherche non ancrée).
    text: Annotated[
        str, Field(min_length=1, max_length=MAX_SEARCH_TEXT_LENGTH, pattern=r"\S")
    ]
    limit: Annotated[int, Field(ge=1, le=MAX_SEARCH_LIMIT)] = 5
    language: Annotated[str | None, Field(min_length=1, max_length=35)] = None
    topics: list[str] = []
    kinds: list[DocumentKind] = []
    document_ids: list[UUID] = []


class CitationResponse(BaseModel):
    document_id: UUID
    document_title: str
    version_id: UUID
    version_number: int
    section_path: list[str]
    page_start: int | None
    page_end: int | None
    source_filename: str


class PassageResponse(BaseModel):
    unit_id: UUID
    unit_type: str
    question: str | None
    text: str
    truncated: bool
    matched_chunk_ids: list[UUID]
    score: float
    citation: CitationResponse


class SearchResponse(BaseModel):
    index_id: UUID
    complete: bool
    passages: list[PassageResponse]
