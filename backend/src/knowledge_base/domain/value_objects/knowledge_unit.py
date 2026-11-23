"""Unité pédagogique indivisible (knowledge-base.md §2, §3.4 ; ADR 0009 §1).

Donnée dérivée immuable, hors agrégat ; ``unit_id`` est déterministe (UUIDv5
de la version, de la politique et du rang), calculé par le segmenteur.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from knowledge_base.domain.value_objects.page_span import PageSpan
from knowledge_base.domain.value_objects.section_path import SectionPath
from knowledge_base.domain.value_objects.structured_document import Block


class UnitType(StrEnum):
    QA = "QA"
    SECTION = "SECTION"
    DEFINITION = "DEFINITION"


@dataclass(frozen=True)
class KnowledgeUnit:
    """Une unité ``QA`` porte sa question (hors de ``blocks``) ; les autres
    n'en ont pas."""

    unit_id: UUID
    ordinal: int
    unit_type: UnitType
    question_text: str | None
    section_path: SectionPath
    page_span: PageSpan
    token_count: int
    blocks: tuple[Block, ...]

    def __post_init__(self) -> None:
        if self.unit_type is UnitType.QA:
            if self.question_text is None or not self.question_text.strip():
                raise ValueError("une unité QA exige une question non vide")
        elif self.question_text is not None:
            raise ValueError(
                f"une unité {self.unit_type} ne porte pas de question : "
                f"{self.question_text!r}"
            )
        if self.ordinal < 0:
            raise ValueError(f"ordinal négatif : {self.ordinal}")
        if self.token_count < 0:
            raise ValueError(f"token_count négatif : {self.token_count}")
