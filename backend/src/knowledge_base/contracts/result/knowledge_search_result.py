"""Résultat du contrat ``KnowledgeSearch`` (knowledge-base.md §14).

Le consommateur fige dans ses propres snapshots le texte et la citation qu'il
utilise (ADR 0007 §4) : une nouvelle version ou une réindexation ne les
modifie pas rétroactivement.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class Citation:
    """Référence stable vers l'original : document, version, section, pages
    (jamais un ``chunk_id``)."""

    document_id: UUID
    document_title: str
    version_id: UUID
    version_number: int
    section_path: tuple[str, ...]
    # Tous deux absents pour un format sans pages (Markdown, texte).
    page_start: int | None
    page_end: int | None
    source_filename: str


@dataclass(frozen=True)
class RetrievedPassage:
    unit_id: UUID
    unit_type: str  # QA, SECTION, DEFINITION
    question: str | None
    # Unité complète, ou fenêtre bornée par ``max_passage_tokens``.
    text: str
    truncated: bool
    matched_chunk_ids: tuple[UUID, ...]
    # Meilleur score de similarité des chunks de l'unité.
    score: float
    citation: Citation


@dataclass(frozen=True)
class KnowledgeSearchResult:
    # Meilleur score d'abord ; au plus ``limit``, une unité au plus une fois.
    passages: tuple[RetrievedPassage, ...]
    # Faux si des passages ont été écartés par le contrôle de visibilité et que
    # le complément n'a pas suffi à en rendre ``limit``.
    complete: bool
    # Génération d'index interrogée (provenance).
    index_id: UUID
