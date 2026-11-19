"""Résultat de ``SearchKnowledge`` : les champs du contrat public
(knowledge-base.md §14), avec les types du domaine. L'adapter
``adapters/controller/contracts/`` le traduit ; le cas d'usage n'importe
jamais ``contracts/``."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from knowledge_base.domain.value_objects.citation import Citation
from knowledge_base.domain.value_objects.knowledge_unit import UnitType


@dataclass(frozen=True)
class FoundPassage:
    unit_id: UUID
    unit_type: UnitType
    question: str | None
    text: str
    truncated: bool
    matched_chunk_ids: tuple[UUID, ...]
    score: float
    citation: Citation


@dataclass(frozen=True)
class SearchKnowledgeResult:
    passages: tuple[FoundPassage, ...]
    # Faux si des unités ont été écartées par le contrôle SQL et que le
    # complément n'a pas suffi à rendre ``limit`` passages.
    complete: bool
    index_id: UUID
