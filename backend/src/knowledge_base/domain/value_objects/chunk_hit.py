"""Chunk trouvé par le moteur vectoriel (knowledge-base.md §12 étape 3).

Le moteur ne rend que des identifiants et un score : le texte, la citation
et la visibilité viennent de PostgreSQL (§12 étapes 4 et 6 ; ADR 0010 §1).
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class ChunkHit:
    chunk_id: UUID
    unit_id: UUID
    # Similarité du chunk à la requête : plus grand = plus proche.
    score: float
