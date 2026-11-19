"""Unité d'une version publiée, relue pour reconstruire un passage
(knowledge-base.md §12 étapes 4 et 6 ; ADR 0009 §6).

Seul le contrôle SQL final (``PublishedUnitReader``) en produit : une
``PublishedUnit`` désigne une unité dont la version est ``PUBLISHED`` et le
document ``ACTIVE`` au moment de la lecture.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from knowledge_base.domain.value_objects.citation import Citation
from knowledge_base.domain.value_objects.knowledge_unit import UnitType


@dataclass(frozen=True)
class UnitChunk:
    chunk_id: UUID
    ordinal_in_unit: int
    # ``contextual_text`` du chunk : en-tête contextuel puis contenu.
    text: str
    token_count: int


@dataclass(frozen=True)
class PublishedUnit:
    unit_id: UUID
    unit_type: UnitType
    question_text: str | None
    # Blocs rendus de l'unité, sans la question.
    text: str
    # Taille des blocs seuls (``knowledge_units.token_count``, comptée à
    # l'extraction) ; la question est ajoutée par ``passage_assembler``.
    blocks_token_count: int
    # Dans l'ordre de lecture (``ordinal_in_unit``).
    chunks: tuple[UnitChunk, ...]
    # Citation de l'unité : chemin de sections et pages de l'unité entière.
    citation: Citation
