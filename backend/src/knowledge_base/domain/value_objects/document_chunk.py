"""Fragment indexable d'une unité (knowledge-base.md §2, §3.4 ; I13).

``contextual_text`` = en-tête contextuel + contenu, **sans** préfixe de modèle
(``passage: `` est ajouté par l'adapter d'embeddings, ADR 0009 §8).
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from knowledge_base.domain.value_objects.citation import Citation


@dataclass(frozen=True)
class DocumentChunk:
    chunk_id: UUID
    unit_id: UUID
    ordinal_in_unit: int
    contextual_text: str
    chunk_hash: str
    token_count: int
    # Obligatoire et sans défaut : I13 est porté par la construction.
    citation: Citation
