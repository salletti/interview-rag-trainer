"""Schémas HTTP de la recherche (S5), distincts du contrat ``KnowledgeSearch``
et du cas d'usage : noms des champs du contrat (knowledge-base.md §14), UUID
en texte."""

from __future__ import annotations

from typing import Annotated, Final
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from knowledge_base.domain.value_objects.document_kind import DocumentKind

# Plafond HTTP du nombre de passages : au-delà, la réponse (jusqu'à
# ``max_passage_tokens`` par passage) n'est plus un contexte exploitable.
MAX_LIMIT: Final = 50


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # pattern : au moins un caractère non blanc (recherche non ancrée).
    text: Annotated[str, Field(min_length=1, pattern=r"\S")]
    limit: Annotated[int, Field(ge=1, le=MAX_LIMIT)] = 5
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
